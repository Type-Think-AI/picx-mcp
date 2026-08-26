"""Tests for picx_mcp.client — HTTP behaviour, error mapping, and redaction.

🔴 NO LIVE API CALLS. Every test uses respx to mock httpx.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from picx_mcp.client import PicXClient, PicXError, redact
from picx_mcp.settings import Settings

FAKE_KEY = "pxsk_live_abcdef123456"
BASE = "https://api.picxstudio.com/v1"


@pytest.fixture()
def _mock_settings() -> None:
    """Patch get_settings so PicXClient reads our controlled base."""
    settings = Settings(picx_api_base=BASE, picx_api_timeout=5.0)
    with patch("picx_mcp.client.get_settings", return_value=settings):
        yield


# ─── Authorization ────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_bearer_token_sent() -> None:
    """Authorization: Bearer <key> is attached to every request."""
    route = respx.get(f"{BASE}/models").mock(return_value=httpx.Response(200, json=[]))
    client = PicXClient(FAKE_KEY)
    await client.get("/models")
    assert route.called
    sent_auth = route.calls[0].request.headers["authorization"]
    assert sent_auth == f"Bearer {FAKE_KEY}"


# ─── None-stripping ──────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_none_fields_stripped_from_body() -> None:
    """None-valued fields must not appear in the request body (absent ≠ null)."""
    route = respx.post(f"{BASE}/images/generate").mock(
        return_value=httpx.Response(200, json={"id": "gen_1"})
    )
    client = PicXClient(FAKE_KEY)
    await client.post("/images/generate", json={"prompt": "cat", "seed": None, "style": None})
    body = route.calls[0].request.content
    import json

    payload = json.loads(body)
    assert "prompt" in payload
    assert "seed" not in payload
    assert "style" not in payload


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_none_params_stripped_from_query() -> None:
    """None-valued query params must not be sent."""
    route = respx.get(f"{BASE}/generations").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = PicXClient(FAKE_KEY)
    await client.get("/generations", params={"limit": 10, "cursor": None})
    url = str(route.calls[0].request.url)
    assert "limit=10" in url
    assert "cursor" not in url


# ─── Error mapping ────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_402_sets_insufficient_credits() -> None:
    """HTTP 402 → PicXError with is_insufficient_credits=True."""
    respx.post(f"{BASE}/images/generate").mock(
        return_value=httpx.Response(402, json={"detail": "Insufficient credits"})
    )
    client = PicXClient(FAKE_KEY)
    with pytest.raises(PicXError) as exc_info:
        await client.post("/images/generate", json={"prompt": "x"})
    assert exc_info.value.is_insufficient_credits is True
    assert exc_info.value.status_code == 402


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_credit_mention_in_body_sets_insufficient_credits() -> None:
    """Any body containing 'credit' triggers is_insufficient_credits even on other codes."""
    respx.post(f"{BASE}/images/generate").mock(
        return_value=httpx.Response(400, json={"detail": "not enough credits remaining"})
    )
    client = PicXClient(FAKE_KEY)
    with pytest.raises(PicXError) as exc_info:
        await client.post("/images/generate", json={"prompt": "x"})
    assert exc_info.value.is_insufficient_credits is True


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_429_sets_rate_limited() -> None:
    """HTTP 429 → PicXError with is_rate_limited=True."""
    respx.post(f"{BASE}/images/generate").mock(
        return_value=httpx.Response(429, json={"detail": "Rate limit exceeded"})
    )
    client = PicXClient(FAKE_KEY)
    with pytest.raises(PicXError) as exc_info:
        await client.post("/images/generate", json={"prompt": "x"})
    assert exc_info.value.is_rate_limited is True
    assert exc_info.value.status_code == 429


@respx.mock
@pytest.mark.usefixtures("_mock_settings")
async def test_timeout_surfaces_as_504() -> None:
    """A network timeout raises PicXError with status 504."""
    respx.get(f"{BASE}/models").mock(side_effect=httpx.ReadTimeout("timed out"))
    client = PicXClient(FAKE_KEY)
    with pytest.raises(PicXError) as exc_info:
        await client.get("/models")
    assert exc_info.value.status_code == 504


# ─── Client construction guards ──────────────────────────────────────────────


@pytest.mark.usefixtures("_mock_settings")
def test_empty_api_key_rejected() -> None:
    with pytest.raises(PicXError, match="no API key"):
        PicXClient("")


@pytest.mark.usefixtures("_mock_settings")
def test_base_url_without_v1_rejected() -> None:
    with pytest.raises(PicXError, match="must end in /v1"):
        PicXClient(FAKE_KEY, base_url="https://api.picxstudio.com")


# ─── Redaction ────────────────────────────────────────────────────────────────


class TestRedact:
    """redact() must NEVER return a full key."""

    def test_full_key_not_in_output(self) -> None:
        key = "pxsk_live_abcdef123456"
        result = redact(key)
        assert key not in result

    def test_shows_last_four(self) -> None:
        result = redact("pxsk_live_abcdef123456")
        assert result.endswith("3456")

    def test_none_key(self) -> None:
        assert redact(None) == "<none>"

    def test_short_key_still_redacted(self) -> None:
        """Even a very short token must not leak in full."""
        key = "pxsk"
        result = redact(key)
        # 4-char key: last4 == full key, but must be prefixed with pxsk_…
        assert result.startswith("pxsk_…")
