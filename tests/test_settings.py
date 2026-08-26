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
    """oauth_configured is only True when ALL FOUR fields are present."""

    def test_false_when_none_set(self, fake_settings: Settings) -> None:
        assert fake_settings.oauth_configured is False

    def test_true_when_all_set(self, oauth_settings: Settings) -> None:
        assert oauth_settings.oauth_configured is True

    def test_false_when_client_id_missing(self) -> None:
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id=None,
            google_client_secret="sec",
            jwt_signing_key="jwtkey123456789012345678901234",
            storage_encryption_key="enckey12345678901234567890123456",
        )
        assert s.oauth_configured is False

    def test_false_when_client_secret_missing(self) -> None:
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id="cid",
            google_client_secret=None,
            jwt_signing_key="jwtkey123456789012345678901234",
            storage_encryption_key="enckey12345678901234567890123456",
        )
        assert s.oauth_configured is False

    def test_false_when_jwt_key_missing(self) -> None:
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id="cid",
            google_client_secret="sec",
            jwt_signing_key=None,
            storage_encryption_key="enckey12345678901234567890123456",
        )
        assert s.oauth_configured is False

    def test_false_when_encryption_key_missing(self) -> None:
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id="cid",
            google_client_secret="sec",
            jwt_signing_key="jwtkey123456789012345678901234",
            storage_encryption_key=None,
        )
        assert s.oauth_configured is False

    def test_false_when_empty_string(self) -> None:
        """Empty strings are falsy — oauth_configured must be False."""
        s = Settings(
            picx_api_base="https://api.picxstudio.com/v1",
            google_client_id="",
            google_client_secret="sec",
            jwt_signing_key="jwtkey123456789012345678901234",
            storage_encryption_key="enckey12345678901234567890123456",
        )
        assert s.oauth_configured is False
