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

2. **OAuth (Phase 5).** The caller sends an OAuth bearer token. It is exchanged
   server-side for the user's session key, so this service still never holds a
   real `pxsk_`, and revoking a grant leaves the user's own API keys working.

Plane 1 is checked first because it needs no round trip.
"""

from __future__ import annotations

from .client import PicXClient, PicXError
from .settings import get_settings


def _bearer_from_headers() -> str | None:
    """Pull a bearer token out of the live request, or None outside one."""
    try:
        from fastmcp.server.dependencies import get_http_headers
    except ImportError:  # pragma: no cover
        return None

    try:
        headers = get_http_headers() or {}
    except Exception:
        # Raised when there is no active HTTP request — e.g. stdio transport, or
        # a background task running outside the originating request.
        return None

    # Header lookup must be case-insensitive; the mapping may or may not be.
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _oauth_token() -> str | None:
    """The verified OAuth access token, when an auth provider is configured."""
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:  # pragma: no cover
        return None
    try:
        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    return getattr(token, "token", None) or str(token)


def resolve_api_key() -> str:
    """The PicX credential for this request, or raise a clear 401.

    Order matters. A `pxsk_` presented directly is used as-is; anything else is
    treated as an OAuth token needing exchange.
    """
    raw = _bearer_from_headers()
    if raw and raw.startswith("pxsk_"):
        return raw

    token = _oauth_token() or raw
    if token:
        # Phase 5. Deliberately not silently falling through to a service-wide
        # key: that would make one user's call spend another user's credits.
        raise PicXError(
            "OAuth authentication is not enabled on this deployment yet. "
            "Send a PicX API key instead: Authorization: Bearer pxsk_… "
            "(get one at https://ai.picxstudio.com/api)",
            status_code=501,
        )

    raise PicXError(
        "No credential supplied. Send Authorization: Bearer pxsk_… — "
        "get a key at https://ai.picxstudio.com/api",
        status_code=401,
    )


def get_client() -> PicXClient:
    """A `/v1` client bound to this request's caller. Use this in every tool."""
    return PicXClient(resolve_api_key(), base_url=get_settings().picx_api_base)
