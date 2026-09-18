"""Tests for picx_mcp.tools.templates — search + fetch against the live /v1 contract.

🔴 NO LIVE API CALLS. PicXClient.get is replaced with a fake that records params.
"""

from __future__ import annotations

from typing import Any

import pytest

from picx_mcp.client import PicXError
from picx_mcp.tools import templates


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
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        return self.response


def _load(monkeypatch: pytest.MonkeyPatch, response: Any):
    """Register templates tools bound to a fake client; return (tools, fake)."""
    # Clear the module cache so tests don't see each other's responses.
    templates._cache.clear()
    mcp = _FakeMCP()
    templates.register(mcp)
    fake = _FakeClient(response)
    monkeypatch.setattr(templates, "get_client", lambda: fake)
    return mcp.tools, fake


_SEARCH_RESPONSE = {
    "templates": [
        {
            "id": "tpl_1",
            "title": "Cinematic portrait",
            "prompt": "a cinematic portrait, 85mm, soft light",
            "media_type": "image",
            "topic": None,
            "tags": ["cinematic", "portrait"],
            "target_model": "flux-1.1-pro",
            "preview_url": "https://cdn/p.png",
            "thumbnail_url": "https://cdn/t.png",
            "is_featured": True,
            "likes": 42,
        },
        {
            "id": "tpl_2",
            "title": "Premium anime",
            "prompt": None,  # gated
            "media_type": "image",
            "topic": None,
            "tags": ["anime"],
            "target_model": "flux-1.1-pro",
            "preview_url": "https://cdn/p2.png",
            "thumbnail_url": "https://cdn/t2.png",
            "is_featured": False,
            "likes": 7,
        },
    ],
    "total": 32,  # estimate: offset(0) + len(30) + 1... here just illustrative
    "limit": 30,
    "offset": 0,
}


# ── search: param building ──────────────────────────────────────────────────


class TestSearchParams:
    async def test_defaults(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"]()
        path, params = fake.calls[0]
        assert path == "/templates"
        assert params["limit"] == 30
        assert params["offset"] == 0
        # None-valued optional filters are present as None; the client strips them.
        assert params["q"] is None

    async def test_limit_clamped(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"](limit=999)
        assert fake.calls[0][1]["limit"] == 100

    async def test_offset_floored(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"](offset=-5)
        assert fake.calls[0][1]["offset"] == 0

    async def test_bad_media_type_rejected(self, monkeypatch) -> None:
        tools, _ = _load(monkeypatch, _SEARCH_RESPONSE)
        with pytest.raises(PicXError, match="media_type must be"):
            await tools["picx_search_templates"](media_type="audio")

    async def test_tags_repeatable_list(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"](tags=["a", "b"])
        assert fake.calls[0][1]["tags"] == ["a", "b"]

    async def test_featured_false_is_forwarded_not_dropped(self, monkeypatch) -> None:
        """featured=False must be present so it can serialise as 'false'."""
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"](featured=False, trending=False)
        params = fake.calls[0][1]
        assert params["featured"] is False
        assert params["trending"] is False

    async def test_booleans_omitted_when_none(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"]()
        params = fake.calls[0][1]
        assert "featured" not in params
        assert "trending" not in params


# ── search: response passthrough + documented behaviours ────────────────────


class TestSearchResponse:
    async def test_returns_full_envelope(self, monkeypatch) -> None:
        tools, _ = _load(monkeypatch, _SEARCH_RESPONSE)
        out = await tools["picx_search_templates"](q="portrait")
        assert set(out) >= {"templates", "total", "limit", "offset"}
        assert len(out["templates"]) == 2

    async def test_gated_prompt_null_preserved(self, monkeypatch) -> None:
        """A null prompt (premium/gated) is passed through untouched."""
        tools, _ = _load(monkeypatch, _SEARCH_RESPONSE)
        out = await tools["picx_search_templates"]()
        assert out["templates"][1]["prompt"] is None

    async def test_topic_field_always_null(self, monkeypatch) -> None:
        tools, _ = _load(monkeypatch, _SEARCH_RESPONSE)
        out = await tools["picx_search_templates"](topic="portraits")
        assert all(t["topic"] is None for t in out["templates"])

    async def test_cache_hit_avoids_second_call(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE)
        await tools["picx_search_templates"](q="x")
        await tools["picx_search_templates"](q="x")
        assert len(fake.calls) == 1  # second served from cache


# ── get one ─────────────────────────────────────────────────────────────────


class TestGetTemplate:
    async def test_fetch_by_id(self, monkeypatch) -> None:
        tools, fake = _load(monkeypatch, _SEARCH_RESPONSE["templates"][0])
        out = await tools["picx_get_template"](template_id="tpl_1")
        assert fake.calls[0][0] == "/templates/tpl_1"
        assert out["id"] == "tpl_1"

    async def test_empty_id_rejected(self, monkeypatch) -> None:
        tools, _ = _load(monkeypatch, {})
        with pytest.raises(PicXError, match="template_id is required"):
            await tools["picx_get_template"](template_id="  ")
