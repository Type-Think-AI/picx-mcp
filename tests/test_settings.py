"""Tests for picx_mcp.settings — startup validation and property logic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from picx_mcp.settings import Settings


class TestPicxApiBase:
    """picx_api_base MUST end in /v1 — the bare host 404s."""

    def test_valid_base_accepted(self) -> None:
        s = Settings(picx_api_base="https://api.picxstudio.com/v1")
        assert s.picx_api_base == "https://api.picxstudio.com/v1"

    def test_bare_host_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must end in /v1"):
            Settings(picx_api_base="https://api.picxstudio.com")

    def test_v2_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must end in /v1"):
            Settings(picx_api_base="https://api.picxstudio.com/v2")

    def test_random_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must end in /v1"):
            Settings(picx_api_base="https://api.picxstudio.com/api")

    def test_trailing_slash_normalised(self) -> None:
        """A trailing slash is stripped so URL joins work predictably."""
        s = Settings(picx_api_base="https://api.picxstudio.com/v1/")
        assert not s.picx_api_base.endswith("/")
        assert s.picx_api_base == "https://api.picxstudio.com/v1"

    def test_multiple_trailing_slashes_normalised(self) -> None:
        s = Settings(picx_api_base="https://api.picxstudio.com/v1///")
        assert s.picx_api_base == "https://api.picxstudio.com/v1"


class TestOAuthConfigured:
    """oauth_configured is True iff the OAuth issuer is set (resource-server topology).

    The old four-field precondition (google_client_id/secret, jwt_signing_key,
    storage_encryption_key) belonged to the withdrawn OAuthProxy issuer role.
    As a pure resource server the connector needs only the issuer — from it it
    derives the JWKS URI, the expected `iss`, and the authorization server it
    names in protected-resource metadata.
    """

    def test_false_when_issuer_unset(self, fake_settings: Settings) -> None:
        assert fake_settings.oauth_configured is False

    def test_true_when_issuer_set(self, oauth_settings: Settings) -> None:
        assert oauth_settings.oauth_configured is True

    def test_issuer_alone_is_sufficient(self) -> None:
        """No other secret is required — the old four fields are irrelevant now."""
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            picx_auth_issuer="https://api.picxstudio.com",
        )
        assert s.oauth_configured is True

    def test_old_secrets_without_issuer_do_not_enable_oauth(self) -> None:
        """Setting the withdrawn-topology secrets but not the issuer stays fail-closed."""
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id="cid",
            google_client_secret="sec",
            jwt_signing_key="jwtkey123456789012345678901234",
            storage_encryption_key="enckey12345678901234567890123456",
            picx_auth_issuer=None,
        )
        assert s.oauth_configured is False

    def test_false_when_issuer_empty_string(self) -> None:
        """An empty issuer is falsy — oauth_configured must be False (fail-closed)."""
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            picx_auth_issuer="",
        )
        assert s.oauth_configured is False


class TestOpenAIAppsChallengeToken:
    """openai_apps_challenge_token is None by default (route deploys inert)."""

    def test_unset_by_default(self) -> None:
        s = Settings(picx_api_base="https://api.picxstudio.com/v1")
        assert s.openai_apps_challenge_token is None

    def test_accepts_a_token_value(self) -> None:
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            openai_apps_challenge_token="tok-123",
        )
        assert s.openai_apps_challenge_token == "tok-123"
