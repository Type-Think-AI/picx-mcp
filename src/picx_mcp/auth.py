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
## Plane 2 — OAuth (Phase 5), REVISED TOPOLOGY 2026-09-18
##
##   picx-studio is the OAuth 2.1 authorization server. This connector is a
##   PURE RESOURCE SERVER: it verifies bearer tokens picx-studio minted and
##   issues nothing. It holds no upstream client credential and no signing key,
##   so its blast radius is a token verifier, not a token factory.
##
##   When `settings.oauth_configured` is True (i.e. the issuer is set), the
##   server presents a RemoteAuthProvider that:
##
##     • verifies each token's signature against the issuer's JWKS,
##     • asserts the `iss` claim equals the configured issuer,
##     • asserts the audience equals this connector's published `resource`
##       value (settings.picx_mcp_base_url),
##     • advertises RFC 9728 protected-resource metadata naming picx-studio as
##       the authorization server, and emits the 401 WWW-Authenticate challenge.
##
##   On a verified token the `sub` claim is exchanged server-side for a scoped,
##   revocable PicX session key via POST /api/internal/session-keys/resolve, so
##   the connector still never holds a real `pxsk_` in this path and revoking a
##   grant leaves the user's own API keys working.
##
##   What used to live here and moved to picx-studio: the Google upstream, CIMD,
##   PKCE, `resource` echo, client storage, and the JWT signing key. Those are
##   authorization-server behaviours; a resource server does none of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .client import PicXError
from .settings import get_settings

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import RemoteAuthProvider

logger = logging.getLogger(__name__)


def build_auth() -> "RemoteAuthProvider | None":
    """Return a configured resource-server auth provider, or None for passthrough.

    Returns
    -------
    RemoteAuthProvider | None
        - None  → Phase 2 (API-key passthrough). The server exposes no OAuth
          surface; the MCP client supplies a pxsk_ key per-request.
        - RemoteAuthProvider → Phase 5 (OAuth, resource-server role). Verifies
          tokens picx-studio minted against its JWKS and advertises RFC 9728
          protected-resource metadata. Serves NO authorization-server metadata
          (that is picx-studio's now).
    """
    settings = get_settings()

    if not settings.oauth_configured:
        logger.info(
            "OAuth not configured (picx_auth_issuer unset). Running in API-key "
            "passthrough mode — no OAuth surface advertised."
        )
        return None

    # ── Deferred imports: only needed when OAuth is active ────────────────────
    # Signatures verified against the installed fastmcp==4.0.0b3 before writing
    # (this is the exact step whose omission made the original GoogleProvider
    # code unrunnable):
    #
    #   JWTVerifier.__init__(self, *, public_key=None, jwks_uri=None,
    #       issuer: str | list[str] | None = None,
    #       audience: str | list[str] | None = None, algorithm=None,
    #       required_scopes=None, base_url=None, ssrf_safe=False,
    #       http_client=None)   ← all keyword-only
    #
    #   RemoteAuthProvider.__init__(self, token_verifier: TokenVerifier,
    #       authorization_servers: list[AnyHttpUrl],
    #       base_url: AnyHttpUrl | str, scopes_supported: list[str] | None = None,
    #       resource_base_url=None, resource_name=None,
    #       resource_documentation=None, challenge_scopes=None)
    try:
        from pydantic import AnyHttpUrl

        from fastmcp.server.auth.auth import RemoteAuthProvider
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "fastmcp.server.auth RemoteAuthProvider / JWTVerifier not found. "
            "Ensure fastmcp >= 4.0.0 is installed."
        ) from exc

    issuer = settings.picx_auth_issuer.rstrip("/")  # type: ignore[union-attr]

    # Both spellings of this connector's resource identifier, because the two
    # ends of the flow disagree on the trailing slash and `aud` is compared as
    # an exact string.
    #
    # RemoteAuthProvider publishes protected-resource metadata built from
    # `base_url` via pydantic's AnyHttpUrl, which NORMALISES by appending a
    # trailing slash — so the document advertises
    # `resource: "https://mcp.picxstudio.com/"` while settings.picx_mcp_base_url
    # is the bare "https://mcp.picxstudio.com".
    #
    # Per OpenAI's authentication guide, ChatGPT "sends this exact value as the
    # `resource` query parameter during OAuth", and picx-studio echoes that
    # value verbatim into the token's `aud` (RFC 8707). So a real ChatGPT grant
    # arrives with aud="https://mcp.picxstudio.com/" and, verified against the
    # bare form alone, EVERY tool call 401s immediately after a login that
    # looked successful. Our own connect.py dodged this by doing
    # MCP_URL.rstrip("/") client-side; ChatGPT has no such workaround because it
    # uses whatever this server publishes.
    #
    # Accepting both is the fix rather than picking one, because the published
    # metadata is generated by FastMCP and the echo is performed by a separate
    # service: pinning either side alone leaves the other free to drift. Both
    # values denote the same resource, so this widens spelling, not audience —
    # a token minted for any OTHER resource is still rejected.
    audiences = [settings.picx_mcp_base_url.rstrip("/")]
    audiences.append(audiences[0] + "/")

    # The verifier is the whole of the resource server's trust decision:
    #   • jwks_uri — where the issuer publishes its signing keys (RFC 8414
    #     discovery puts JWKS at {issuer}/.well-known/jwks.json).
    #   • issuer   — the `iss` claim must equal this exactly (verbatim compare).
    #   • audience — the `aud` claim must be this connector's published
    #     `resource` value, in either spelling. See above.
    verifier = JWTVerifier(
        jwks_uri=f"{issuer}/.well-known/jwks.json",
        issuer=issuer,
        audience=audiences,
        # SSRF-safe JWKS fetch: HTTPS-only, blocks private/link-local targets.
        # FastMCP's documented production default for remote JWKS fetches. The
        # issuer is operator-configured (not attacker-controlled), so the risk
        # is low, but this closes the last-mile gap of a misconfigured issuer
        # pointing the fetch at an internal address.
        ssrf_safe=True,
    )

    # RemoteAuthProvider serves ONLY /.well-known/oauth-protected-resource — it
    # does not serve /.well-known/oauth-authorization-server, which is correct:
    # a pure resource server does not describe an authorization server it does
    # not run. `authorization_servers` names picx-studio as where to get a token.
    # Declared so the published metadata does not advertise `scopes_supported: []`.
    # OpenAI's guide describes this field as what "helps ChatGPT explain the
    # permissions it is going to ask the user for" — an empty list tells a client
    # nothing, so it cannot request the right scopes or render an accurate consent
    # screen. Mirrors SESSION_KEY_SCOPES on picx-studio (the source of truth) and
    # the values in quota.TOOL_SCOPES; kept as a literal here because importing
    # quota would be circular (quota -> context -> auth).
    provider = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=settings.picx_mcp_base_url,
        scopes_supported=[
            "images:generate",
            "images:edit",
            "videos:generate",
            "uploads:write",
        ],
    )

    logger.info(
        "OAuth configured: RemoteAuthProvider (resource server), issuer=%s, "
        "audience=%s",
        issuer,
        settings.picx_mcp_base_url,
    )
    return provider


# ─────────────────────────────────────────────────────────────────────────────
# Token → Session Key exchange (Phase 5)
# ─────────────────────────────────────────────────────────────────────────────


async def exchange_token_for_session_key(oauth_subject: str, scopes: list[str] | None = None) -> str:
    """Exchange a verified OAuth subject claim for a scoped PicX session key.

    Resolves the caller's identity to a scoped, revocable PicX session key via
    the PicX API's internal exchange route. The returned key is what gets
    forwarded to /v1 on every tool call — the MCP server never holds a real
    pxsk_ in this path, and revoking the grant leaves the user's own API keys
    working.

    Parameters
    ----------
    oauth_subject : str
        The identity provider's stable subject claim (the `sub` of the verified
        access token). Matched against User.oauth_sub on the PicX side. Never an
        email — emails are reassignable, a subject claim is not.
    scopes : list[str] | None
        Optional narrowing. Intersected with the session-key scope set on the
        PicX side, never unioned. Omit for the full session-key scope set.

    Returns
    -------
    str
        A scoped PicX session key usable against /v1. Returned exactly once by
        the API and NOT logged here.

    Raises
    ------
    PicXError
        401 — the internal secret is missing or wrong (bad connector config).
        404 — no PicX account is linked to this identity (surfaced clearly).
        503 — the internal exchange API is disabled on the PicX deployment.

    Notes
    -----
    Contract (verified live on prod, picx-studio commit 85f5b8cf):
        POST {picx_api_base without /v1}/api/internal/session-keys/resolve
        Header: X-PicX-Internal-Secret: <settings.picx_internal_secret>
        Body:   { "oauth_subject": "<sub>", "scopes": [...] }
        200:    { "session_key": "...", "expires_at": "...", "scopes": [...] }

    The route lives on the /api surface, NOT /v1 — it mints a credential for an
    account without presenting that account's own credentials, so it is gated on
    a shared secret and deliberately absent from the public /v1 surface.
    """
    import httpx

    settings = get_settings()

    if not settings.picx_internal_secret:
        # Fail closed: without the shared secret we cannot call the exchange and
        # must not fall through to any service-wide credential.
        raise PicXError(
            "OAuth session-key exchange is not configured on this deployment "
            "(picx_internal_secret unset).",
            status_code=503,
        )

    # The internal route is on the /api surface, not /v1. picx_api_base is
    # pinned to end in /v1, so strip that one segment to reach the API host root.
    api_root = settings.picx_api_base.rstrip("/")
    if api_root.endswith("/v1"):
        api_root = api_root[: -len("/v1")]
    url = f"{api_root}/api/internal/session-keys/resolve"

    body: dict[str, object] = {"oauth_subject": oauth_subject}
    if scopes is not None:
        body["scopes"] = scopes

    async with httpx.AsyncClient(timeout=settings.picx_api_timeout) as http:
        try:
            resp = await http.post(
                url,
                headers={
                    "X-PicX-Internal-Secret": settings.picx_internal_secret,
                    "Content-Type": "application/json",
                    "User-Agent": "picx-mcp/0.1.0",
                },
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise PicXError("timed out resolving session key", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise PicXError(f"network error resolving session key: {exc}", status_code=502) from exc

    if resp.status_code == 401:
        # Bad or missing internal secret — a connector misconfiguration, not a
        # user problem. Do not echo the secret or the subject.
        raise PicXError(
            "OAuth session-key exchange rejected: invalid internal credential "
            "(the connector's picx_internal_secret does not match the PicX API).",
            status_code=401,
        )
    if resp.status_code == 404:
        raise PicXError(
            "No PicX account is linked to this identity. Sign up or link your "
            "account at https://ai.picxstudio.com before using this connector.",
            status_code=404,
        )
    if resp.status_code == 503:
        raise PicXError(
            "The PicX internal session-key API is disabled on this deployment.",
            status_code=503,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text[:500]
        raise PicXError(str(detail), status_code=resp.status_code)

    data = resp.json()
    session_key = data.get("session_key")
    if not session_key:
        raise PicXError(
            "session-key exchange returned no session_key", status_code=502
        )
    # Deliberately NOT logged — the raw key is returned exactly once and is a
    # live credential.
    return session_key
