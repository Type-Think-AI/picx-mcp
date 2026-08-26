"""Video generation tools.

Exposes text-to-video, image-to-video, and reference-to-video generation via the
PicX `/v1/videos/generate` endpoint, plus a read-only poll tool for checking
generation status.

## Mode matrix (exposed)

| Mode        | Required fields              | Notes                        |
|-------------|------------------------------|------------------------------|
| text        | prompt                       | Default mode                 |
| image       | prompt, image_url            | First-frame driven           |
| reference   | prompt, reference_urls (≤10) | Style/motion reference clips |

## Modes NOT exposed

frames, extend, lipsync, edit — require fields the MCP parameter schema cannot
safely serialize without dedicated validation (start_frame_url, end_frame_url,
source_video_url, audio_url). Exposing a mode whose required fields are absent
from the schema causes a confusing runtime 422 from the API rather than a clear
client-side error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ..client import PicXError
from ..context import get_client

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ── Tool registration ─────────────────────────────────────────────────────────


def register(mcp: "FastMCP") -> None:
    """Register video tools on the MCP server instance."""

    # ──────────────────────────────────────────────────────────────────────────
    # picx_generate_video
    #
    # task=True registers this as a background task via the MCP tasks extension.
    # Video generation takes minutes; the tasks extension means the agent needs
    # no polling logic — the server pushes status updates.
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool(
        task=True,
        description=(
            "Start a video generation (text-to-video, image-to-video, or "
            "reference-to-video). ALWAYS returns 202 immediately with "
            "{id, status, type, model, poll_url, events_url} — the video renders "
            "in the background. Poll picx_get_generation with the returned id "
            "every 10-15 seconds until status is 'completed' or 'failed'. "
            "Costs credits (amount depends on duration and resolution). "
            "Do NOT call this for image generation — use picx_generate_image instead."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def picx_generate_video(
        prompt: str,
        model: str | None = None,
        mode: Literal["text", "image", "reference"] = "text",
        duration: int = 5,
        resolution: Literal["480p", "720p", "1080p"] = "720p",
        aspect_ratio: str | None = None,
        sound: bool = True,
        image_url: str | None = None,
        reference_urls: list[str] | None = None,
    ) -> dict:
        """Generate a video. Returns immediately with a generation ID to poll."""

        # ── Per-mode validation ───────────────────────────────────────────────
        if not prompt or not prompt.strip():
            raise PicXError("prompt is required for all video modes", status_code=400)

        if mode == "image":
            if not image_url:
                raise PicXError(
                    "image_url is required when mode='image'", status_code=400
                )
        elif mode == "reference":
            if not reference_urls or len(reference_urls) == 0:
                raise PicXError(
                    "reference_urls (1-10 URLs) is required when mode='reference'",
                    status_code=400,
                )
            if len(reference_urls) > 10:
                raise PicXError(
                    f"reference_urls accepts at most 10 URLs (got {len(reference_urls)})",
                    status_code=400,
                )

        if not (1 <= duration <= 60):
            raise PicXError(
                f"duration must be 1-60 seconds (got {duration})", status_code=400
            )

        # ── Build request body ────────────────────────────────────────────────

        # ╔══════════════════════════════════════════════════════════════════════╗
        # ║ MODES NOT EXPOSED                                                    ║
        # ║                                                                      ║
        # ║ The PicX API supports 7 modes total. These 4 are intentionally       ║
        # ║ omitted because their required fields are not in this tool's schema: ║
        # ║                                                                      ║
        # ║   • frames  — needs start_frame_url + end_frame_url                 ║
        # ║   • extend  — needs source_video_url                                ║
        # ║   • lipsync — needs source_video_url + audio_url                    ║
        # ║               (also the ONLY prompt-optional mode)                   ║
        # ║   • edit    — needs source_video_url                                ║
        # ║                                                                      ║
        # ║ Exposing a mode whose mandatory fields aren't in the MCP parameter   ║
        # ║ schema means the client cannot serialize them. The API returns a     ║
        # ║ confusing 422 "field required" error rather than a clear client-side ║
        # ║ validation message. Add these modes only when their fields are added ║
        # ║ to the tool parameters above.                                        ║
        # ╚══════════════════════════════════════════════════════════════════════╝

        body: dict[str, Any] = {
            "prompt": prompt.strip(),
            "mode": mode,
            "duration": duration,
            "resolution": resolution,
            "sound": sound,
        }
        if model:
            body["model"] = model
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if mode == "image":
            body["image_url"] = image_url
        if mode == "reference":
            body["reference_urls"] = reference_urls

        # ── Fire request ──────────────────────────────────────────────────────
        client = get_client()
        result = await client.post("/videos/generate", json=body)

        # Normalise response — the API always returns 202 with these fields.
        return {
            "id": result["id"],
            "status": result.get("status", "pending"),
            "type": result.get("type", "video"),
            "model": result.get("model"),
            "poll_url": result.get("poll_url"),
            "events_url": result.get("events_url"),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # picx_get_generation (video poll)
    # ──────────────────────────────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "Check the status of a generation (image or video). Returns the current "
            "status, output_url (when complete), credits_used, and any error_message. "
            "Use this to poll after picx_generate_video — call every 10-15 seconds "
            "until status is 'completed' or 'failed'. "
            "Free — does not cost credits."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
        },
    )
    async def picx_get_generation(
        generation_id: str,
    ) -> dict:
        """Poll a generation by ID. Returns status, output_url, credits_used, error_message."""
        if not generation_id or not generation_id.strip():
            raise PicXError("generation_id is required", status_code=400)

        client = get_client()
        result = await client.get(f"/generations/{generation_id.strip()}")

        return {
            "id": result.get("id", generation_id),
            "status": result.get("status"),
            "output_url": result.get("output_url"),
            "credits_used": result.get("credits_used"),
            "error_message": result.get("error_message"),
        }
