"""Tests for picx_mcp.tools.videos — the seven-mode validator + body builder.

🔴 NO LIVE API CALLS. The PicXClient is replaced with a fake that records the
posted body instead of hitting the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from picx_mcp.client import PicXError
from picx_mcp.tools import videos


# ── Harness: capture the tool closures without a real FastMCP ──────────────────


class _FakeMCP:
    """Records @mcp.tool()-decorated functions by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class _FakeClient:
    """Stands in for PicXClient — records the last posted body."""

    def __init__(self) -> None:
        self.last_path: str | None = None
        self.last_body: dict[str, Any] | None = None

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.last_path = path
        self.last_body = json
        return {
            "id": "vid_123",
            "status": "pending",
            "type": "video",
            "model": "kling-2",
            "poll_url": "/v1/generations/vid_123",
            "events_url": "/v1/generations/vid_123/events",
        }


@pytest.fixture()
def generate_video(monkeypatch: pytest.MonkeyPatch):
    """Return (fn, fake_client). fn is picx_generate_video bound to a fake client."""
    mcp = _FakeMCP()
    videos.register(mcp)
    fn = mcp.tools["picx_generate_video"]
    fake = _FakeClient()
    async def _fake_get_client():
        return fake
    monkeypatch.setattr(videos, "get_client", _fake_get_client)
    return fn, fake


# ── prompt requirement (lipsync is the sole exception) ─────────────────────────


class TestPromptRequirement:
    @pytest.mark.parametrize("mode", ["text", "image", "reference", "frames", "extend", "edit"])
    async def test_prompt_required_for_non_lipsync(self, generate_video, mode: str) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="prompt is required"):
            # Supply the other required fields so we isolate the prompt check.
            await fn(
                prompt="",
                mode=mode,
                image_url="https://x/i.png",
                reference_urls=["https://x/r.mp4"],
                start_frame_url="https://x/s.png",
                source_video_url="https://x/v.mp4",
            )

    async def test_prompt_optional_for_lipsync(self, generate_video) -> None:
        fn, fake = generate_video
        out = await fn(
            prompt=None,
            mode="lipsync",
            source_video_url="https://x/v.mp4",
            audio_url="https://x/a.mp3",
        )
        assert out["id"] == "vid_123"
        assert "prompt" not in fake.last_body  # empty prompt not forwarded


# ── per-mode required-field validation ─────────────────────────────────────────


class TestModeValidation:
    async def test_image_needs_image_url(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="image_url is required"):
            await fn(prompt="p", mode="image")

    async def test_reference_needs_urls(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="reference_urls"):
            await fn(prompt="p", mode="reference")

    async def test_reference_max_ten(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="at most 10"):
            await fn(prompt="p", mode="reference", reference_urls=[f"https://x/{i}" for i in range(11)])

    async def test_frames_needs_start(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="start_frame_url is required"):
            await fn(prompt="p", mode="frames")

    async def test_extend_needs_source(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="source_video_url is required"):
            await fn(prompt="p", mode="extend")

    async def test_lipsync_needs_both(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="BOTH source_video_url and audio_url"):
            await fn(mode="lipsync", source_video_url="https://x/v.mp4")

    async def test_edit_needs_both(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="BOTH source_video_url and image_url"):
            await fn(prompt="p", mode="edit", source_video_url="https://x/v.mp4")


# ── body building: only the chosen mode's fields are forwarded ─────────────────


class TestBodyBuilding:
    async def test_frames_body(self, generate_video) -> None:
        fn, fake = generate_video
        await fn(
            prompt="p",
            mode="frames",
            start_frame_url="https://x/s.png",
            end_frame_url="https://x/e.png",
        )
        body = fake.last_body
        assert body["mode"] == "frames"
        assert body["start_frame_url"] == "https://x/s.png"
        assert body["end_frame_url"] == "https://x/e.png"
        assert "source_video_url" not in body
        assert "image_url" not in body

    async def test_frames_end_optional(self, generate_video) -> None:
        fn, fake = generate_video
        await fn(prompt="p", mode="frames", start_frame_url="https://x/s.png")
        assert "end_frame_url" not in fake.last_body

    async def test_edit_body(self, generate_video) -> None:
        fn, fake = generate_video
        await fn(
            prompt="p",
            mode="edit",
            source_video_url="https://x/v.mp4",
            image_url="https://x/i.png",
        )
        body = fake.last_body
        assert body["source_video_url"] == "https://x/v.mp4"
        assert body["image_url"] == "https://x/i.png"

    async def test_lipsync_body_has_no_prompt_when_empty(self, generate_video) -> None:
        fn, fake = generate_video
        await fn(mode="lipsync", source_video_url="https://x/v.mp4", audio_url="https://x/a.mp3")
        body = fake.last_body
        assert body["mode"] == "lipsync"
        assert body["source_video_url"] == "https://x/v.mp4"
        assert body["audio_url"] == "https://x/a.mp3"
        assert "prompt" not in body

    async def test_stale_url_from_other_mode_not_leaked(self, generate_video) -> None:
        """A source_video_url passed to an image render must NOT reach the body."""
        fn, fake = generate_video
        await fn(prompt="p", mode="image", image_url="https://x/i.png", source_video_url="https://x/v.mp4")
        assert "source_video_url" not in fake.last_body
        assert fake.last_body["image_url"] == "https://x/i.png"

    async def test_duration_bounds(self, generate_video) -> None:
        fn, _ = generate_video
        with pytest.raises(PicXError, match="duration must be 1-60"):
            await fn(prompt="p", mode="text", duration=0)
