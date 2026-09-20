"""Per-request credential resolution — the one place that answers "who is calling?".

Every agent that wrote a tool module needed this and correctly refused to guess
at it. The answer, verified against the installed `fastmcp==4.0.0b3`:

    from fastmcp.server.dependencies import get_http_headers, get_access_token

`get_http_headers()` returns the incoming request headers inside a tool call, and
`get_access_token()` returns the OAuth access token when an auth provider is
configured. Both are ContextVar-backed, so they work without threading a `ctx`
argument through every signature.

## The two auth planes, resolved here

1. **API key passthrough (available now).** The caller sends
   `Authorization: Bearer pxsk_…`. We forward it to `/v1` unchanged. This service
   stores no credential — a key it never holds is a key it cannot leak.

2. **OAuth (Phase 5, wired 2026-09-19).** The caller sends an OAuth bearer token.
   `resolve_api_key` verifies it was already checked by the auth middleware (this
   function only runs at all past a 401), reads the verified `sub` claim off
   `get_access_token()`, and exchanges it server-side for the user's PicX session
   key via `exchange_token_for_session_key`. This service still never holds a
   real `pxsk_` long-term, and revoking a grant leaves the user's own API keys
   working.

Plane 1 is checked first because it needs no round trip.
"""

from __future__ import annotations

import time

from . import store
from .auth import exchange_token_for_session_key
from .client import PicXClient, PicXError
from .settings import get_settings


def _bearer_from_headers() -> str | None:
    """Pull a bearer token out of the live request, or None outside one.

    FastMCP 4 in stateless_http mode: `get_http_headers()` returns an empty dict
    because headers aren't propagated via that ContextVar in stateless mode. The
    actual path is through `get_http_request()` which returns the Starlette
    Request object, or failing that, through the FastMCPRequestContext.request.
    """
    # Path 1: get_http_request() — returns the Starlette Request directly
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        if req is not None:
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip() or None
    except Exception:
        pass

    # Path 2: get_http_headers() — works in some transports
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers() or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
    except Exception:
        pass

    return None


def _verified_access_token():
    """The `AccessToken` FastMCP's auth middleware already verified, or None.

    Returns the object (not just the raw string) because the field this module
    needs — `subject` — is the JWT's verified `sub` claim, populated by
    `JWTVerifier.load_access_token` (`subject=claims.get("sub")`). Re-deriving
    that by decoding the raw token here would duplicate signature verification
    that has already happened and, worse, could silently drift from it (e.g. if
    the middleware's issuer/audience checks ever changed independent of a second
    decode site). One verification, one place that trusts its result.
    """
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:  # pragma: no cover
        return None
    try:
        return get_access_token()
    except Exception:
        return None


def _oauth_token() -> str | None:
    """The verified OAuth access token's raw string, when configured."""
    access_token = _verified_access_token()
    if access_token is None:
        return None
    return getattr(access_token, "token", None) or str(access_token)


# ─────────────────────────────────────────────────────────────────────────────
# Session-key cache
# ─────────────────────────────────────────────────────────────────────────────
#
# `exchange_token_for_session_key` mints a FRESH PicX session key on every call
# and, on the PicX side, `resolve_oauth_grant_key` deactivates the user's
# previous grant key as part of minting the new one (see picx-studio's
# key_service.py). Re-exchanging on every tool call would therefore invalidate
# the credential a PRECEDING call in the same turn is still using — a chat turn
# that calls two tools would have the first tool's key deactivated by the
# second tool's exchange, mid-turn, for no reason: the OAuth access token itself
# is unchanged between those calls, so there is nothing to re-resolve.
#
# Cached per OAuth access token (the JWT string, which already encodes its own
# expiry), NOT per PicX user, so a revoked/rotated OAuth token can never serve a
# stale cache entry under a reused key. TTL is a independent, conservative
# safety net — short enough that a mid-request revocation on the PicX side is
# noticed soon, long enough that a multi-tool-call turn shares one exchange.
_SESSION_KEY_CACHE: dict[str, tuple[float, str]] = {}
_SESSION_KEY_CACHE_TTL_SECONDS = 60.0


# ─────────────────────────────────────────────────────────────────────────────
# The cache is SHARED (Valkey) with a per-process fallback
# ─────────────────────────────────────────────────────────────────────────────
#
# A per-process dict was actively wrong here, not merely suboptimal. Because the
# exchange ROTATES (picx-studio retires the previous grant key on every call) and
# this deployment runs instance_count: 2 with stateless_http (no sticky
# sessions), a turn landing A -> B -> A served a key replica B had already
# retired, and `/v1` 401'd it. See store.py's module docstring for the full
# sequence; that module is the shared tier.
#
# The in-process dict is KEPT as a fallback for when Valkey is unreachable:
# degrading to the old behaviour during an outage is better than failing the
# call, and `PicXClient`'s retry-on-401 makes even that case self-healing.


async def _cached_session_key(oauth_token: str) -> str | None:
    """Shared cache first, then this process's own. None on a miss."""
    shared = await store.get_session_key(oauth_token)
    if shared:
        return shared
    entry = _SESSION_KEY_CACHE.get(oauth_token)
    if entry is None:
        return None
    expires_at, session_key = entry
    if time.monotonic() >= expires_at:
        _SESSION_KEY_CACHE.pop(oauth_token, None)
        return None
    return session_key


async def _store_session_key(oauth_token: str, session_key: str) -> None:
    """Write through to both tiers.

    The local tier is written even when the shared write succeeds, so a Valkey
    outage mid-turn falls back to something useful instead of re-exchanging (and
    therefore retiring) on every subsequent call.
    """
    await store.set_session_key(
        oauth_token, session_key, int(_SESSION_KEY_CACHE_TTL_SECONDS)
    )
    _SESSION_KEY_CACHE[oauth_token] = (
        time.monotonic() + _SESSION_KEY_CACHE_TTL_SECONDS,
        session_key,
    )


async def invalidate_session_key(oauth_token: str) -> None:
    """Forget this token's session key everywhere, then let the next call re-exchange.

    Invoked when `/v1` rejects the key we sent, which on a multi-replica
    deployment almost always means another replica's exchange retired it. Clears
    the shared tier too — a local-only delete would leave every OTHER replica
    replaying the same dead credential until its own TTL ran out.
    """
    _SESSION_KEY_CACHE.pop(oauth_token, None)
    await store.invalidate_session_key(oauth_token)


def clear_session_key_cache() -> None:
    """Drop this process's cached exchange results. Tests only."""
    _SESSION_KEY_CACHE.clear()


async def resolve_api_key() -> str:
    """The PicX credential for this request, or raise a clear error.

    Order matters. A `pxsk_` presented directly is used as-is; anything else is
    treated as an OAuth token needing exchange. Async because the OAuth branch
    makes a real network call to picx-studio's internal exchange route — every
    call site is inside an `async def` tool function already, so this only
    changes the call site to `await resolve_api_key()` / `await get_client()`.
    """
    raw = _bearer_from_headers()
    if raw and raw.startswith("pxsk_"):
        return raw

    access_token = _verified_access_token()
    token_str = (
        getattr(access_token, "token", None) if access_token is not None else None
    ) or raw
    if not token_str:
        raise PicXError(
            "No credential supplied. Send Authorization: Bearer pxsk_… — "
            "get a key at https://ai.picxstudio.com/api",
            status_code=401,
            # Only meaningful on an OAuth deployment, where `www_authenticate()`
            # resolves to a real challenge; on a passthrough deployment it
            # returns None and this falls through as a plain error. Normally
            # unreachable under OAuth (the auth middleware 401s a tokenless
            # request before any tool runs), but if a request ever does arrive
            # here credential-less, a linking prompt is the right answer.
            oauth_challenge="invalid_token",
        )

    if access_token is None or not getattr(access_token, "subject", None):
        # A bearer token is present but this deployment has no OAuth provider
        # configured (build_auth() returned None), so nothing verified it and
        # there is no `subject` claim to exchange. Distinguishing this from "no
        # credential at all" is what makes the 501 actionable rather than a
        # generic auth failure.
        raise PicXError(
            "OAuth authentication is not enabled on this deployment yet. "
            "Send a PicX API key instead: Authorization: Bearer pxsk_… "
            "(get one at https://ai.picxstudio.com/api)",
            status_code=501,
        )

    cached = await _cached_session_key(token_str)
    if cached is not None:
        return cached

    # `access_token.scopes` is whatever the AS granted (already narrowed to the
    # SESSION_KEY_SCOPES vocabulary on picx-studio's side at token-mint time —
    # see oauth_as/scopes.py). Passed through rather than omitted so the
    # resolved session key cannot exceed what the user actually consented to;
    # `exchange_token_for_session_key` intersects it again server-side regardless.
    session_key = await exchange_token_for_session_key(
        access_token.subject,
        scopes=list(access_token.scopes) if access_token.scopes else None,
    )
    await _store_session_key(token_str, session_key)
    return session_key


async def get_client() -> PicXClient:
    """A `/v1` client bound to this request's caller. Use this in every tool.

    On the OAuth path the client is given `on_auth_failure`, a one-shot callback
    it invokes when `/v1` answers 401. That is the residual case the shared cache
    cannot close by itself: two replicas that miss the cache at the SAME moment
    both exchange, and the second exchange retires the first's key. Rather than
    serialise every exchange behind a distributed lock — which would add a new
    way for the whole auth path to stall — we let the loser notice and recover:
    forget the key everywhere, exchange once more, retry the call.

    Deliberately NOT wired for a direct `pxsk_` caller. That key is the user's
    own and a 401 on it means it is genuinely invalid or revoked; re-exchanging
    would be wrong (there is nothing to exchange) and retrying would just repeat
    a rejection.
    """
    api_key = await resolve_api_key()
    settings = get_settings()

    if api_key.startswith("pxsk_") and _verified_access_token() is None:
        # Direct API-key passthrough — no exchange exists to retry.
        return PicXClient(api_key, base_url=settings.picx_api_base)

    access_token = _verified_access_token()
    token_str = getattr(access_token, "token", None) if access_token else None
    if not token_str:
        return PicXClient(api_key, base_url=settings.picx_api_base)

    async def _reauth() -> str | None:
        """Forget the retired key and mint a fresh one. None if we cannot."""
        await invalidate_session_key(token_str)
        subject = getattr(access_token, "subject", None)
        if not subject:
            return None
        try:
            fresh = await exchange_token_for_session_key(
                subject,
                scopes=list(access_token.scopes) if access_token.scopes else None,
            )
        except PicXError:
            # The exchange itself is failing (bad internal secret, unlinked
            # account, API down). Let the ORIGINAL 401 surface rather than
            # replacing it with a second, less relevant error.
            return None
        await _store_session_key(token_str, fresh)
        return fresh

    return PicXClient(
        api_key, base_url=settings.picx_api_base, on_auth_failure=_reauth
    )
