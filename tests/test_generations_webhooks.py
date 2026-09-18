"""Tests for the new generation sub-resource + webhook delivery tools.

🔴 NO LIVE API CALLS. respx mocks the SSE stream; a fake client mocks the rest.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from picx_mcp.client import PicXError
from picx_mcp.settings import Settings
from picx_mcp.tools import generations, webhooks

BASE = "https://api.picxstudio.com/v1"


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def get(self, path: str, **kw: Any) -> Any:
        self.calls.append(("GET", path))
        return self.response

    async def post(self, path: str, **kw: Any) -> Any:
        self.calls.append(("POST", path))
        return self.response


@pytest.fixture()
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = Settings(picx_api_base=BASE, picx_api_timeout=5.0)
    monkeypatch.setattr(generations, "get_settings", lambda: s)
    async def _fake_resolve_api_key():
        return "pxsk_test"
    monkeypatch.setattr(generations, "resolve_api_key", _fake_resolve_api_key)
    return s


def _load_generations(monkeypatch: pytest.MonkeyPatch, response: Any):
    mcp = _FakeMCP()
    generations.register(mcp)
    fake = _FakeClient(response)
    async def _fake_get_client():
        return fake
    monkeypatch.setattr(generations, "get_client", _fake_get_client)
    return mcp.tools, fake


def _load_webhooks(monkeypatch: pytest.MonkeyPatch, response: Any):
    mcp = _FakeMCP()
    webhooks.register(mcp)
    fake = _FakeClient(response)
    async def _fake_get_client():
        return fake
    monkeypatch.setattr(webhooks, "get_client", _fake_get_client)
    return mcp.tools, fake


# ── SSE events reader ─────────────────────────────────────────────────────────


class TestGenerationEvents:
    @respx.mock
    async def test_reads_until_terminal(self, monkeypatch, _settings) -> None:
        sse = (
            'data: {"status": "processing", "progress": 10}\n\n'
            'data: {"status": "processing", "progress": 80}\n\n'
            'data: {"status": "completed", "output_url": "https://cdn/v.mp4"}\n\n'
        )
        respx.get(f"{BASE}/generations/vid_1/events").mock(
            return_value=httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )
        tools, _ = _load_generations(monkeypatch, None)
        out = await tools["picx_get_generation_events"](generation_id="vid_1", timeout_seconds=5)
        assert out["generation_id"] == "vid_1"
        assert out["terminal"] is True
        assert out["timed_out"] is False
        assert out["count"] == 3
        assert out["events"][-1]["status"] == "completed"

    @respx.mock
    async def test_stops_on_max_events(self, monkeypatch, _settings) -> None:
        sse = "".join(f'data: {{"progress": {i}}}\n\n' for i in range(10))
        respx.get(f"{BASE}/generations/vid_2/events").mock(
            return_value=httpx.Response(200, text=sse)
        )
        tools, _ = _load_generations(monkeypatch, None)
        out = await tools["picx_get_generation_events"](
            generation_id="vid_2", timeout_seconds=5, max_events=3
        )
        assert out["count"] == 3
        assert out["terminal"] is False

    @respx.mock
    async def test_http_error_raises(self, monkeypatch, _settings) -> None:
        respx.get(f"{BASE}/generations/nope/events").mock(
            return_value=httpx.Response(404, json={"detail": "not found"})
        )
        tools, _ = _load_generations(monkeypatch, None)
        with pytest.raises(PicXError) as exc:
            await tools["picx_get_generation_events"](generation_id="nope", timeout_seconds=5)
        assert exc.value.status_code == 404

    async def test_empty_id_rejected(self, monkeypatch, _settings) -> None:
        tools, _ = _load_generations(monkeypatch, None)
        with pytest.raises(PicXError, match="generation_id is required"):
            await tools["picx_get_generation_events"](generation_id="")


# ── generation deliveries ───────────────────────────────────────────────────


class TestGenerationDeliveries:
    async def test_calls_correct_path(self, monkeypatch, _settings) -> None:
        tools, fake = _load_generations(monkeypatch, {"deliveries": []})
        await tools["picx_get_generation_deliveries"](generation_id="vid_9")
        assert ("GET", "/generations/vid_9/deliveries") in fake.calls

    async def test_empty_id_rejected(self, monkeypatch, _settings) -> None:
        tools, _ = _load_generations(monkeypatch, {})
        with pytest.raises(PicXError, match="generation_id is required"):
            await tools["picx_get_generation_deliveries"](generation_id="  ")


# ── webhook tools ────────────────────────────────────────────────────────────


class TestWebhookTools:
    async def test_get_deliveries_path(self, monkeypatch) -> None:
        tools, fake = _load_webhooks(monkeypatch, {"deliveries": []})
        await tools["picx_get_webhook_deliveries"](webhook_id="wh_1")
        assert ("GET", "/webhooks/wh_1/deliveries") in fake.calls

    async def test_redeliver_path(self, monkeypatch) -> None:
        tools, fake = _load_webhooks(monkeypatch, {"id": "del_new"})
        await tools["picx_redeliver_webhook"](delivery_id="del_1")
        assert ("POST", "/webhooks/deliveries/del_1/redeliver") in fake.calls

    async def test_webhook_id_required(self, monkeypatch) -> None:
        tools, _ = _load_webhooks(monkeypatch, {})
        with pytest.raises(PicXError, match="webhook_id is required"):
            await tools["picx_get_webhook_deliveries"](webhook_id="")

    async def test_delivery_id_required(self, monkeypatch) -> None:
        tools, _ = _load_webhooks(monkeypatch, {})
        with pytest.raises(PicXError, match="delivery_id is required"):
            await tools["picx_redeliver_webhook"](delivery_id="")
