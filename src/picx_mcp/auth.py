"""OAuth auth provider factory for the PicX MCP server.

# 🚨 KNOWN BLOCKER — uploads:write scope missing
#
# SESSION_KEY_SCOPES in the PicX API omits `uploads:write`, and because
# /v1/images/edit rejects data URIs, every edit flow needs an upload first.
# With OAuth resolving to a session key, EVERY upload will 403 until that
# one-line backend fix lands (adding "uploads:write" to the session key
# scope set). This is Phase 5 backend work on the PicX API side.


## Two-Plane Auth Design
## ─────────────────────
##
## Plane 1 — API key passthrough (Phase 2, current default)
##
##   The MCP client passes a `pxsk_` key per-request. This server forwards it
##   verbatim to /v1 and never stores it. No credential is held server-side,
##   so compromise of the MCP server leaks nothing beyond in-flight memory.
##
## Plane 2 — OAuth (Phase 5)
##
##   When `settings.oauth_configured` is True, the server presents a FastMCP
##   OAuthProxy backed by Google Sign-In. On successful auth the issued access
##   token is exchanged server-side for a PicX session key via
##   `ApiKeyService.resolve_session_key_id`. The session key is scoped &
##   rotatable:
##
##     • Revoking a grant invalidates only the session key — the user's own
##       pxsk_ API keys keep working.
##     • The MCP server NEVER holds a real pxsk_ in the OAuth path.
##     • Session keys inherit per-session credit ceilings independently of the
##       account's daily cap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .settings import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def build_auth():
    """Return a configured auth provider, or None for API-key passthrough mode.

    Returns
    -------
    OAuthProxy | None
        - None  → Phase 2 (API-key passthrough). The server exposes no OAuth
          surface; the MCP client supplies a pxsk_ key per-request.
        - OAuthProxy → Phase 5 (OAuth). Google Sign-In front-door, session key
          resolution backend.
    """
    settings = get_settings()

    if not settings.oauth_configured:
        logger.info(
            "OAuth not configured (missing one of: google_client_id, "
            "google_client_secret, jwt_signing_key, storage_encryption_key). "
            "Running in API-key passthrough mode."
        )
        return None

    # ── Deferred imports: these are only needed when OAuth is active ──────────
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "cryptography package is required for OAuth mode. "
            "Install it with: pip install 'picx-mcp[oauth]'"
        ) from exc

    try:
        from fastmcp.server.auth import OAuthProxy
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "fastmcp.server.auth.OAuthProxy not found. "
            "Ensure fastmcp >= 4.0.0 is installed."
        ) from exc

    # TODO: Check if fastmcp.server.auth.providers.google exists.
    # As of writing, only GitHubProvider is confirmed at
    # fastmcp.server.auth.providers.github. If a GoogleProvider ships later,
    # prefer it over raw OAuthProxy for tighter scope/claim mapping.

    try:
        from key_value.aio.stores.redis import RedisStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "key_value package is required for OAuth mode (encrypted client storage). "
            "Install it with: pip install 'picx-mcp[oauth]'"
        ) from exc

    # ── Build encrypted client storage ────────────────────────────────────────
    # Without encryption, upstream OAuth tokens (Google refresh tokens) would be
    # stored in PLAINTEXT in Redis. The Fernet wrapper encrypts at rest.
    fernet = Fernet(settings.storage_encryption_key.encode())  # type: ignore[union-attr]
    redis_store = RedisStore(url=settings.redis_url)
    client_storage = FernetEncryptionWrapper(key_value=redis_store, fernet=fernet)

    # ── Construct OAuthProxy ──────────────────────────────────────────────────
    proxy = OAuthProxy(
        client_id=settings.google_client_id,  # type: ignore[arg-type]
        client_secret=settings.google_client_secret,  # type: ignore[arg-type]
        jwt_signing_key=settings.jwt_signing_key,  # type: ignore[arg-type]
        client_storage=client_storage,
        base_url=settings.picx_mcp_base_url,
    )

    logger.info(
        "OAuth configured: Google provider, encrypted Redis storage, "
        "base_url=%s",
        settings.picx_mcp_base_url,
    )
    return proxy


# ─────────────────────────────────────────────────────────────────────────────
# Token → Session Key exchange (Phase 5 backend work — STUB)
# ─────────────────────────────────────────────────────────────────────────────


async def exchange_token_for_session_key(access_token: str) -> str:
    """Exchange an OAuth-issued access token for a PicX session key.

    This resolves the MCP access token to a scoped session key via the PicX
    API's `ApiKeyService.resolve_session_key_id`. The session key is what gets
    forwarded to /v1 on every tool call — the MCP server never holds a real
    pxsk_ in this path.

    Parameters
    ----------
    access_token : str
        The JWT access token issued by this server's OAuthProxy after the user
        completes Google Sign-In.

    Returns
    -------
    str
        A scoped PicX session key (sk_…) usable against /v1.

    Raises
    ------
    NotImplementedError
        Always — the PicX-side endpoint does not exist yet. This is Phase 5
        backend work: a new route on the PicX API that accepts an OAuth subject
        claim and returns a scoped session key.

    Notes
    -----
    The PicX API endpoint to build:
        POST /api/internal/session-keys/resolve
        Body: { "oauth_subject": "<google-sub>", "scopes": [...] }
        Returns: { "session_key": "sk_...", "expires_at": "..." }

    This endpoint must be internal-only (mTLS or shared secret between MCP
    server and PicX API), never exposed on the public /v1 surface.
    """
    # Phase 5 backend work — the PicX API does not expose this endpoint yet.
    # When it does, implementation will:
    #   1. Decode `access_token` to extract the Google subject claim
    #   2. POST to PicX internal endpoint with subject + requested scopes
    #   3. Return the scoped session key
    #   4. Cache the mapping (subject → session_key) with TTL matching key expiry
    raise NotImplementedError(
        "exchange_token_for_session_key requires a PicX API endpoint that does not "
        "exist yet (POST /api/internal/session-keys/resolve). This is Phase 5 "
        "backend work. See docstring for the contract."
    )
