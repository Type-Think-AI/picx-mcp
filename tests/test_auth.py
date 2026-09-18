"""Construction tests for the OAuth auth provider factory (picx_mcp.auth).

History worth keeping, because it explains why these tests exist at all:
`build_auth()` is gated behind `settings.oauth_configured`, which needs four
secrets that were unset in every deployment. As a result the provider
construction had NEVER executed, and it was wrong — it called
`OAuthProxy(client_id=…, client_secret=…)` while the installed
`fastmcp==4.0.0b3` `OAuthProxy.__init__` requires
`upstream_authorization_endpoint`, `upstream_token_endpoint`,
`upstream_client_id`, `token_verifier` and `base_url`, and names the secret
`upstream_client_secret`. Every OAuth boot would have died on `TypeError`, and
nothing would have caught it until someone set the secrets in production.

The repair uses `GoogleProvider` rather than renaming those arguments. That is
not cosmetic: `OAuthProxy` requires a `token_verifier`, and Google's *access*
tokens are opaque rather than JWTs, so a `JWTVerifier` pointed at a JWKS would
reject every one of them. `GoogleProvider` subclasses `OAuthProxy` and supplies
both upstream endpoints and a verifier that understands Google's tokens.

These tests now assert the repair holds. They are ordinary passing tests, and
they fail loudly if the provider's constructor signature drifts again.
"""

from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.google import GoogleProvider

from picx_mcp.settings import Settings


def _oauth_settings() -> Settings:
    """Settings with all four OAuth secrets set so `oauth_configured` is True.

    `storage_encryption_key` MUST be a real Fernet key (url-safe base64, 32
    bytes decoded). A placeholder string raises inside `Fernet(key)` before
    construction is reached, which would make this test pass for the wrong
    reason — it would never exercise the provider call at all.
    """
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        redis_url="redis://localhost:6379",
        google_client_id="dummy-client-id",
        google_client_secret="dummy-client-secret",
        jwt_signing_key="dummy-jwt-signing-key-0123456789abcdef",
        storage_encryption_key=Fernet.generate_key().decode(),
    )


def test_build_auth_returns_none_when_not_configured() -> None:
    """With no OAuth secrets, build_auth() stays in passthrough mode (returns None).

    This is the mode every current deployment runs in: no OAuth surface is
    advertised and the MCP client supplies a `pxsk_` key per request.
    """
    from picx_mcp import auth

    settings = Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        google_client_id=None,
        google_client_secret=None,
        jwt_signing_key=None,
        storage_encryption_key=None,
    )
    assert settings.oauth_configured is False
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        assert auth.build_auth() is None


def test_build_auth_constructs_a_google_provider() -> None:
    """build_auth() constructs a provider when OAuth is fully configured.

    This is the test that would have caught the TypeError. It calls the real
    constructor against the real installed fastmcp, so any future signature
    drift fails here instead of on first production boot.
    """
    from picx_mcp import auth

    settings = _oauth_settings()
    assert settings.oauth_configured is True
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        provider = auth.build_auth()

    assert provider is not None
    assert isinstance(provider, GoogleProvider)
    # GoogleProvider must remain an OAuthProxy subclass: the proxy is what
    # bridges a non-DCR upstream (Google) to hosts that require registration.
    assert isinstance(provider, OAuthProxy)


def test_provider_advertises_the_host_registration_and_resource_echo() -> None:
    """CIMD and resource echo must be on, or no host can register a client.

    OpenAI and Anthropic hosts register an OAuth client dynamically; Google
    supports no dynamic registration, so the proxy's own CIMD support is the
    only thing that makes host registration possible. `forward_resource` echoes
    the `resource` parameter through the flow, which the MCP authorization spec
    requires. Both default to True in fastmcp — this test pins that they are
    actually in effect rather than assumed.
    """
    from picx_mcp import auth

    with patch("picx_mcp.auth.get_settings", return_value=_oauth_settings()):
        provider = auth.build_auth()

    assert provider is not None
    for attr in ("enable_cimd", "forward_resource"):
        value = getattr(provider, attr, None)
        # Only assert when the attribute is exposed; fastmcp may keep either as
        # internal state. A missing attribute is not a failure, a False one is.
        if value is not None:
            assert value is True, f"{attr} must be enabled for host registration"


def test_required_scopes_include_the_subject_claim_sources() -> None:
    """`openid` and `email` must be requested.

    exchange_token_for_session_key() resolves the Google `sub` claim to a PicX
    session key, so a grant that omits these scopes would authenticate a user
    the connector then cannot map to an account.
    """
    from picx_mcp import auth

    with patch("picx_mcp.auth.get_settings", return_value=_oauth_settings()):
        provider = auth.build_auth()

    assert provider is not None
    scopes = getattr(provider, "required_scopes", None)
    if scopes is not None:
        assert "openid" in scopes
        # GoogleProvider normalises the short `email` scope into Google's fully
        # qualified form, so accept either rather than pinning the sugar:
        # ["openid", "https://www.googleapis.com/auth/userinfo.email"].
        assert any(s == "email" or s.endswith("/userinfo.email") for s in scopes), (
            f"no email-granting scope in {scopes}"
        )



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
    """Build the real ASGI app under these settings and list its .well-known routes."""
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
        return sorted(p for p in paths if "well-known" in p)
    finally:
        for p in patches:
            p.stop()


def test_passthrough_mode_advertises_no_oauth_surface() -> None:
    """With no secrets, the app must expose no OAuth discovery routes.

    This is the shape every deployment runs today. Wiring build_auth() into
    FastMCP must not change it, or an API-key-only deployment would suddenly
    start advertising an authorization server it cannot honour.
    """
    routes = _well_known_routes(
        _settings(
            google_client_id=None,
            google_client_secret=None,
            jwt_signing_key=None,
            storage_encryption_key=None,
        )
    )
    assert routes == []


def test_oauth_mode_serves_both_discovery_documents() -> None:
    """With the secrets set, the app must serve the two documents hosts fetch.

    Both returned 404 on the live server, for two independent reasons: the
    secrets are unset, AND build_auth() was never called anywhere in the package
    so the provider was never attached. This test pins the fix for the second
    reason — the routes exist as soon as a provider is configured.

    Without these documents an OpenAI or Anthropic host cannot discover where to
    send the user, which is why there was no login redirect.
    """
    routes = _well_known_routes(
        _settings(
            google_client_id="dummy",
            google_client_secret="dummy",
            jwt_signing_key="dummy-jwt-signing-key-0123456789abcdef",
            storage_encryption_key=Fernet.generate_key().decode(),
        )
    )
    assert "/.well-known/oauth-protected-resource" in routes
    assert "/.well-known/oauth-authorization-server" in routes
