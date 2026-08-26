"""Shared fixtures — environment isolation + fake Settings.

Every test in this suite runs against mocked config and a monkeypatched env.
No real .env or real API key is ever read.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from picx_mcp.settings import Settings


FAKE_API_KEY = "pxsk_test_fake000000000000000000"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any test from accidentally reading a developer's real .env.

    Clears all PICX_* / GOOGLE_* / JWT_* / REDIS_* env vars so pydantic-settings
    cannot pick them up.
    """
    sensitive_prefixes = ("PICX_", "GOOGLE_", "JWT_", "REDIS_", "STORAGE_")
    for key in list(os.environ):
        if any(key.startswith(p) for p in sensitive_prefixes):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def fake_settings() -> Settings:
    """A Settings instance with safe defaults — no real credentials."""
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_api_timeout=5.0,
        picx_api_max_retries=0,
        redis_url="redis://localhost:6379",
        google_client_id=None,
        google_client_secret=None,
        jwt_signing_key=None,
        storage_encryption_key=None,
    )


@pytest.fixture()
def oauth_settings() -> Settings:
    """A Settings with all OAuth fields populated."""
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        google_client_id="cid",
        google_client_secret="csec",
        jwt_signing_key="jwtkey123456789012345678901234",
        storage_encryption_key="enckey12345678901234567890123456",
    )


@pytest.fixture()
def patch_get_settings(fake_settings: Settings):
    """Monkeypatch get_settings so modules under test use the fake."""
    with patch("picx_mcp.settings.get_settings", return_value=fake_settings):
        yield fake_settings
