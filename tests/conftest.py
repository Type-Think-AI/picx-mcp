"""Shared fixtures — environment isolation + fake Settings.

Every test in this suite runs against mocked config and a monkeypatched env.
No real .env or real API key is ever read.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from picx_mcp import store
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


@pytest.fixture(autouse=True)
def _no_shared_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Valkey-backed session-key cache out of every test by default.

    `store` degrades silently when Redis is unreachable, so without this the
    suite would still pass — but every `resolve_api_key()` would attempt a real
    TCP connection to localhost:6379 and wait for it to be refused. That is slow,
    and worse it makes behaviour depend on whether the developer happens to have
    a Redis running, which is precisely the kind of ambient dependency the rest
    of this conftest exists to remove.

    Latching `_unavailable` is the documented "hard failure" path, so this
    exercises the real fallback branch rather than a test-only shortcut. Tests
    that WANT the shared tier patch `store.get_session_key` / `set_session_key`
    with fakes — see test_session_key_rotation.py.
    """
    monkeypatch.setattr(store, "_unavailable", True, raising=False)
    monkeypatch.setattr(store, "_client", None, raising=False)


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
    """A Settings with OAuth enabled (resource-server topology).

    The only precondition now is the issuer; from it the connector derives the
    JWKS URI and expected `iss`, and it names the authorization server in
    protected-resource metadata.
    """
    return Settings(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_auth_issuer="https://api.picxstudio.com",
    )


@pytest.fixture()
def patch_get_settings(fake_settings: Settings):
    """Monkeypatch get_settings so modules under test use the fake."""
    with patch("picx_mcp.settings.get_settings", return_value=fake_settings):
        yield fake_settings
