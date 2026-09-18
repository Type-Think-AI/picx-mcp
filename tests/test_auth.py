"""Construction tests for the OAuth auth provider factory (picx_mcp.auth).

`build_auth()` is gated behind `settings.oauth_configured`, which needs four
secrets that are unset in every real deployment so far. As a result the
`OAuthProxy(...)` call at the bottom of `build_auth()` has NEVER executed, and
it is wrong: it passes `client_id=`, `client_secret=`, `jwt_signing_key=`,
`client_storage=`, `base_url=`, but the installed `fastmcp==4.0.0b3`
`OAuthProxy.__init__` REQUIRES `upstream_authorization_endpoint`,
`upstream_token_endpoint`, `upstream_client_id`, `token_verifier`, `base_url`,
and names the secret `upstream_client_secret`. So the call raises TypeError on
first OAuth boot.

`test_build_auth_constructs` documents that defect. It is marked
xfail(strict=True): it MUST fail against the current auth.py, and will flip to a
hard failure the moment someone repairs build_auth() without updating this test
(strict xfail treats an unexpected pass as a failure). Repairing build_auth()
is a separate planned task (task 4 in the official-plugin-directory spec) that
needs the upstream Google endpoint values decided first — so this lane only
adds the guard, it does not touch auth.py's construction logic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from picx_mcp.settings import Settings


def _oauth_settings() -> Settings:
    """Settings with all four OAuth secrets set so `oauth_configured` is True.

    The storage_encryption_key MUST be a real Fernet key (url-safe base64,
    32 bytes decoded) — otherwise `Fernet(key)` raises before build_auth ever
    reaches the OAuthProxy call, which would mask the signature defect we are
    documenting.
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
    """With no OAuth secrets, build_auth() stays in passthrough mode (returns None)."""
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "build_auth() calls OAuthProxy(client_id=, client_secret=, jwt_signing_key=, "
        "client_storage=, base_url=), but fastmcp==4.0.0b3 OAuthProxy.__init__ requires "
        "upstream_authorization_endpoint, upstream_token_endpoint, upstream_client_id, "
        "token_verifier, base_url and names the secret upstream_client_secret — so the "
        "call raises TypeError. Fixing this is spec task 4 (needs upstream endpoint "
        "values). This xfail flips to a hard failure once build_auth() is repaired."
    ),
)
def test_build_auth_constructs() -> None:
    """build_auth() must construct a provider when OAuth is fully configured.

    Fails today (TypeError: OAuthProxy signature mismatch). That failure IS the
    point — it documents the defect and guards against future signature drift.
    """
    from picx_mcp import auth

    settings = _oauth_settings()
    assert settings.oauth_configured is True
    with patch("picx_mcp.auth.get_settings", return_value=settings):
        provider = auth.build_auth()
    assert provider is not None
