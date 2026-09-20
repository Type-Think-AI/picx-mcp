"""Construction tests for the resource-server auth provider factory (picx_mcp.auth).

History worth keeping, because it explains why these tests exist at all:
`build_auth()` is gated behind `settings.oauth_configured`, which once needed
four secrets that were unset in every deployment. As a result the provider
construction had NEVER executed, and it was wrong — it called
`OAuthProxy(client_id=…, client_secret=…)` while the installed
`fastmcp==4.0.0b3` `OAuthProxy.__init__` required different arguments. Every
OAuth boot would have died on `TypeError`, and nothing would have caught it
until someone set the secrets in production. Commit `290a0df` repaired that to a
`GoogleProvider` and, more consequentially, wired `build_auth()` into
`FastMCP(auth=…)` for the first time.

Then the authorization topology was revised (2026-09-18): picx-studio became the
OAuth 2.1 authorization server, and this connector became a PURE RESOURCE
SERVER. So the provider is re-pointed again — from `GoogleProvider` (issuer,
proxy) to `RemoteAuthProvider` wrapping a `JWTVerifier` (verifier only). These
tests assert the resource-server construction holds and fail loudly if the
constructor signatures drift again. They call the real constructors against the
real installed fastmcp, exactly the check whose absence caused the original bug.
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import patch

from fastmcp.server.auth.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier

from picx_mcp.client import PicXError
from picx_mcp.settings import Settings


def _oauth_settings() -> Settings:
    """Settings with the OAuth issuer set so `oauth_configured` is True.

    Under the resource-server topology the issuer is the ONLY precondition: from
    it the verifier derives the JWKS URI and the expected `iss`, and it names the
    authorization server in protected-resource metadata. The old four secrets are
    no longer required (they belonged to the issuer role picx-studio now owns).
    """
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        redis_url="redis://localhost:6379",
        picx_auth_issuer="https://api.picxstudio.com",
    )


def test_build_auth_returns_none_when_not_configured() -> None:
    """With no issuer, build_auth() stays in passthrough mode (returns None).

    This is the mode every current deployment runs in: no OAuth surface is
    advertised and the MCP client supplies a `pxsk_` key per request.
    """
    from picx_mcp import auth

    settings = Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_auth_issuer=None,
    )
    assert settings.oauth_configured is False
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        assert auth.build_auth() is None


def test_build_auth_constructs_a_resource_server() -> None:
    """build_auth() constructs a RemoteAuthProvider when the issuer is set.

    This is the test that would have caught the original TypeError. It calls the
    real constructors against the real installed fastmcp, so any future signature
    drift fails here instead of on first production boot.
    """
    from picx_mcp import auth

    settings = _oauth_settings()
    assert settings.oauth_configured is True
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        provider = auth.build_auth()

    assert provider is not None
    # A pure resource server: verifies tokens, issues nothing. NOT an OAuthProxy
    # and NOT a GoogleProvider — those were the withdrawn issuer topology.
    assert isinstance(provider, RemoteAuthProvider)


def test_verifier_checks_issuer_jwks_and_audience() -> None:
    """The JWTVerifier must bind to the issuer's JWKS, `iss`, and this resource.

    These three are the whole of the resource server's trust decision:
      • jwks_uri = {issuer}/.well-known/jwks.json (RFC 8414 discovery),
      • issuer   = the configured issuer, compared to the token's `iss`,
      • audience = this connector's published `resource` value, in BOTH
        trailing-slash spellings.

    On the audience: RemoteAuthProvider builds protected-resource metadata from
    `base_url` through pydantic's AnyHttpUrl, which appends a trailing slash, so
    the published document says `resource: "https://mcp.picxstudio.com/"` while
    settings.picx_mcp_base_url is the bare form. ChatGPT sends the published
    value verbatim as the `resource` parameter and picx-studio echoes it into
    `aud`, so a real grant arrives slashed. Verifying the bare form alone made
    every tool call 401 straight after a login that appeared to succeed. Both
    spellings name the same resource, so accepting both widens spelling and not
    audience — hence the explicit assertion that it is exactly these two and
    nothing else.

    A drift in any of them silently widens what tokens are accepted, so pin them.
    """
    from picx_mcp import auth

    with patch("picx_mcp.auth.get_settings", return_value=_oauth_settings()):
        provider = auth.build_auth()

    assert provider is not None
    verifier = provider.token_verifier
    assert isinstance(verifier, JWTVerifier)
    assert verifier.jwks_uri == "https://api.picxstudio.com/.well-known/jwks.json"
    assert verifier.issuer == "https://api.picxstudio.com"
    assert verifier.audience == [
        "https://mcp.picxstudio.com",
        "https://mcp.picxstudio.com/",
    ]


def test_published_scopes_are_not_empty() -> None:
    """Protected-resource metadata must declare the scopes a client can request.

    It previously advertised `scopes_supported: []`. OpenAI's authentication
    guide describes this field as what "helps ChatGPT explain the permissions it
    is going to ask the user for", so an empty list leaves a client unable to
    request the right scopes or render an accurate consent screen. These must
    stay in step with SESSION_KEY_SCOPES on picx-studio and quota.TOOL_SCOPES
    here.
    """
    from picx_mcp import auth

    with patch("picx_mcp.auth.get_settings", return_value=_oauth_settings()):
        provider = auth.build_auth()

    assert provider is not None
    assert sorted(provider.scopes_supported or []) == [
        "images:edit",
        "images:generate",
        "uploads:write",
        "videos:generate",
    ]


def test_provider_names_picx_studio_as_authorization_server() -> None:
    """Protected-resource metadata must name the issuer as its authorization server.

    A resource server issues no tokens; it tells the host where to get one. If
    `authorization_servers` did not name picx-studio, a host could discover the
    connector but never find where to send the user to log in.
    """
    from picx_mcp import auth

    with patch("picx_mcp.auth.get_settings", return_value=_oauth_settings()):
        provider = auth.build_auth()

    assert provider is not None
    servers = [str(s).rstrip("/") for s in provider.authorization_servers]
    assert "https://api.picxstudio.com" in servers


# ─────────────────────────────────────────────────────────────────────────────
# Token → session-key exchange (mocked transport — never hits the network)
# ─────────────────────────────────────────────────────────────────────────────


def _exchange_settings() -> Settings:
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        picx_auth_issuer="https://api.picxstudio.com",
        picx_internal_secret="shared-internal-secret",
    )


class _MockClient:
    """Stand-in for httpx.AsyncClient that records the request and replays a response."""

    captured: dict = {}

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def __call__(self, *args: object, **kwargs: object) -> "_MockClient":
        return self

    async def __aenter__(self) -> "_MockClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        type(self).captured = {"url": url, "headers": headers, "json": json}
        return self._response


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_exchange_posts_to_internal_route_with_secret_and_subject() -> None:
    """A 200 resolves to the session key; the request targets /api (not /v1) with the secret."""
    from picx_mcp import auth

    resp = httpx.Response(
        200,
        json={
            "session_key": "sk_live_abc123",
            "expires_at": "2026-09-19T00:00:00Z",
            "scopes": ["images:generate"],
        },
    )
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
    ):
        key = _run(auth.exchange_token_for_session_key("google-sub-1", ["images:generate"]))

    assert key == "sk_live_abc123"
    # /api surface, NOT /v1 — the /v1 segment must be stripped exactly once.
    assert mock.captured["url"] == "https://api.picxstudio.com/api/internal/session-keys/resolve"
    assert mock.captured["headers"]["X-PicX-Internal-Secret"] == "shared-internal-secret"
    assert mock.captured["json"] == {"oauth_subject": "google-sub-1", "scopes": ["images:generate"]}


def test_exchange_omits_scopes_when_none() -> None:
    """Omitting scopes sends no `scopes` key, so the API applies the full session-key set."""
    from picx_mcp import auth

    resp = httpx.Response(
        200, json={"session_key": "sk_x", "expires_at": "2026-09-19T00:00:00Z", "scopes": []}
    )
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
    ):
        _run(auth.exchange_token_for_session_key("google-sub-2"))

    assert "scopes" not in mock.captured["json"]


def test_exchange_fails_closed_without_internal_secret() -> None:
    """No internal secret configured → 503, and no network call is attempted."""
    from picx_mcp import auth

    settings = Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_auth_issuer="https://api.picxstudio.com",
        picx_internal_secret=None,
    )
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        with pytest.raises(PicXError) as exc:
            _run(auth.exchange_token_for_session_key("google-sub-3"))
    assert exc.value.status_code == 503


def test_exchange_maps_401_to_bad_secret() -> None:
    """A 401 from the API is a connector-config error (wrong internal secret)."""
    from picx_mcp import auth

    resp = httpx.Response(401, json={"detail": {"code": "invalid_internal_secret"}})
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
    ):
        with pytest.raises(PicXError) as exc:
            _run(auth.exchange_token_for_session_key("google-sub-4"))
    assert exc.value.status_code == 401
    assert "internal credential" in str(exc.value)


def test_exchange_maps_404_to_no_linked_account() -> None:
    """A 404 (account_not_found) surfaces a clear no-linked-account message."""
    from picx_mcp import auth

    resp = httpx.Response(404, json={"detail": {"code": "account_not_found"}})
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
    ):
        with pytest.raises(PicXError) as exc:
            _run(auth.exchange_token_for_session_key("google-sub-5"))
    assert exc.value.status_code == 404
    assert "no picx account" in str(exc.value).lower()


def test_exchange_maps_503_to_internal_api_disabled() -> None:
    """A 503 (internal_auth_not_configured) surfaces as the internal API being disabled."""
    from picx_mcp import auth

    resp = httpx.Response(503, json={"detail": {"code": "internal_auth_not_configured"}})
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
    ):
        with pytest.raises(PicXError) as exc:
            _run(auth.exchange_token_for_session_key("google-sub-6"))
    assert exc.value.status_code == 503


def test_exchange_does_not_log_the_session_key(caplog: pytest.LogCaptureFixture) -> None:
    """The raw session key is a live credential returned once — it must not be logged."""
    from picx_mcp import auth

    resp = httpx.Response(
        200,
        json={"session_key": "sk_secret_value", "expires_at": "2026-09-19T00:00:00Z", "scopes": []},
    )
    mock = _MockClient(resp)
    with (
        patch("picx_mcp.auth.get_settings", return_value=_exchange_settings()),
        patch("httpx.AsyncClient", mock),
        caplog.at_level("DEBUG"),
    ):
        _run(auth.exchange_token_for_session_key("google-sub-7"))
    assert "sk_secret_value" not in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Route exposure — driven through the real ASGI app
# ─────────────────────────────────────────────────────────────────────────────


def _settings(**overrides: object) -> Settings:
    base = dict(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        redis_url="redis://localhost:6379",
        request_state_key="x" * 32,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _well_known_routes(settings: Settings) -> list[str]:
    """Build the real ASGI app under these settings and list its OAuth .well-known routes.

    Scoped to OAuth/OpenID discovery paths (``oauth-*`` / ``openid-*``). Other
    ``.well-known`` routes that are not part of the authorization surface — e.g.
    ``/.well-known/openai-apps-challenge``, an inert domain-verification endpoint
    — are deliberately excluded, since this helper exists to assert what OAuth
    surface the connector advertises, not every well-known route it happens to
    register.
    """
    from picx_mcp import server

    targets = (
        "picx_mcp.auth.get_settings",
        "picx_mcp.server.get_settings",
        "picx_mcp.settings.get_settings",
    )
    patches = [patch(t, return_value=settings) for t in targets]
    for p in patches:
        p.start()
    try:
        app = server.build_app()
        paths = {getattr(r, "path", "") for r in getattr(app, "routes", [])}
        return sorted(
            p for p in paths if ("oauth-" in p or "openid-" in p)
        )
    finally:
        for p in patches:
            p.stop()


def test_passthrough_mode_advertises_no_oauth_surface() -> None:
    """With no issuer, the app must expose no OAuth discovery routes.

    This is the shape every deployment runs today. Wiring build_auth() into
    FastMCP must not change it, or an API-key-only deployment would suddenly
    start advertising an authorization surface it cannot honour.
    """
    routes = _well_known_routes(_settings(picx_auth_issuer=None))
    assert routes == []


def test_oauth_mode_serves_only_protected_resource_metadata() -> None:
    """A pure resource server serves protected-resource metadata and NOTHING else.

    This is the point of the topology revision. Under the withdrawn OAuthProxy
    design the connector served BOTH .well-known documents; as a resource server
    it must serve ONLY /.well-known/oauth-protected-resource. Authorization-server
    metadata is picx-studio's responsibility now, so its ABSENCE here is correct
    and is asserted, not tolerated.
    """
    routes = _well_known_routes(_settings(picx_auth_issuer="https://api.picxstudio.com"))
    assert "/.well-known/oauth-protected-resource" in routes
    assert "/.well-known/oauth-authorization-server" not in routes
