"""Video generation tools.

Exposes all seven PicX video generation modes via the `/v1/videos/generate`
endpoint, plus a read-only poll tool for checking generation status.

## Mode matrix (all seven exposed)

| Mode      | Required fields                     | prompt?  | Notes                          |
|-----------|-------------------------------------|----------|--------------------------------|
| text      | prompt                              | required | Default mode. Pure text→video. |
| image     | prompt, image_url                   | required | First-frame driven.            |
| reference | prompt, reference_urls (1-10)       | required | Style/motion reference clips.  |
| frames    | prompt, start_frame_url             | required | end_frame_url optional.        |
| extend    | prompt, source_video_url            | required | Continue an existing clip.     |
| lipsync   | source_video_url, audio_url         | OPTIONAL | Drive a face to speak audio.   |
| edit      | prompt, source_video_url, image_url | required | Edit a clip with an image ref. |

The server-side rule (public_api/schemas.py) is the enum
`^(text|image|reference|frames|extend|lipsync|edit)$` with the per-mode required
fields above. This tool validates them client-side so an LLM gets a clear
message instead of a raw 422. `lipsync` is the only mode where `prompt` is not
required — every other mode needs a non-empty prompt.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any, Literal

from fastmcp.tools.base import ToolResult
from mcp.types import ResourceLink, TextContent

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
            "Generate a brand-new AI video using PicX's models. Use this whenever "
            "the user asks to GENERATE, CREATE, MAKE, or 'AI-generate' a video or "
            "animation that does not need to be a real, pre-existing clip. This "
            "tool IS the video generator — prefer it over any stock-footage or "
            "web-search tool whenever the intent is to produce new video content. "
            "Only use a stock-footage tool if the user explicitly asks for a real, "
            "existing clip or says stock/royalty-free/Pexels/Shutterstock.\n"
            "\n"
            "Pick ONE of seven modes and supply that mode's required fields:\n"
            "  • text      — prompt only. Pure text→video (default).\n"
            "  • image     — prompt + image_url. Animate from a still first frame.\n"
            "  • reference — prompt + reference_urls (1-10). Copy style/motion.\n"
            "  • frames    — prompt + start_frame_url (end_frame_url optional). "
            "Interpolate between a start and (optional) end frame.\n"
            "  • extend    — prompt + source_video_url. Continue an existing clip.\n"
            "  • lipsync   — source_video_url + audio_url. Drive a face in the "
            "video to speak the audio. prompt is OPTIONAL for this mode ONLY.\n"
            "  • edit      — prompt + source_video_url + image_url. Edit a clip "
            "guided by an image reference.\n"
            "\n"
            "All URL fields must be https:// (upload local files with "
            "picx_upload_asset first). ALWAYS returns 202 immediately with "
            "{id, status, type, model, poll_url, events_url} — the video renders "
            "in the background. Poll picx_get_generation with the returned id "
            "every 10-15 seconds until status is 'completed' or 'failed', or read "
            "picx_get_generation_events for a bounded event stream. "
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
        prompt: str | None = None,
        model: str | None = None,
        mode: Literal[
            "text", "image", "reference", "frames", "extend", "lipsync", "edit"
        ] = "text",
        duration: int = 5,
        resolution: Literal["480p", "720p", "1080p"] = "720p",
        aspect_ratio: str | None = None,
        sound: bool = True,
        image_url: str | None = None,
        reference_urls: list[str] | None = None,
        start_frame_url: str | None = None,
        end_frame_url: str | None = None,
        source_video_url: str | None = None,
        audio_url: str | None = None,
    ) -> dict:
        """Generate a video in one of seven modes. Returns a generation ID to poll.

        Args:
            prompt: Text description. Required for every mode EXCEPT lipsync,
                where it is optional (the audio drives the output).
            model: Model id. Omit for the account default.
            mode: One of text | image | reference | frames | extend | lipsync | edit.
            duration: Seconds, 1-60. Default 5.
            resolution: "480p" | "720p" | "1080p". Default "720p".
            aspect_ratio: e.g. "16:9", "9:16", "1:1". Omit for model default.
            sound: Whether to generate audio. Default True.
            image_url: First frame (mode='image') or image reference (mode='edit').
            reference_urls: 1-10 style/motion reference clips (mode='reference').
            start_frame_url: Opening frame (mode='frames', required).
            end_frame_url: Closing frame (mode='frames', optional).
            source_video_url: Existing clip to extend/lipsync/edit
                (modes extend, lipsync, edit — required).
            audio_url: Audio track to lip-sync to (mode='lipsync', required).
        """

        # ── Per-mode validation ───────────────────────────────────────────────
        # lipsync is the ONLY mode where prompt is optional; every other mode
        # requires a non-empty prompt. Mirrors the server enum + per-mode rules
        # in public_api/schemas.py so the agent gets a clear message, not a 422.
        prompt_clean = (prompt or "").strip()

        if mode != "lipsync" and not prompt_clean:
            raise PicXError(
                f"prompt is required and cannot be empty for mode='{mode}' "
                "(only mode='lipsync' allows an empty prompt)",
                status_code=400,
            )

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
        elif mode == "frames":
            if not start_frame_url:
                raise PicXError(
                    "start_frame_url is required when mode='frames' "
                    "(end_frame_url is optional)",
                    status_code=400,
                )
        elif mode == "extend":
            if not source_video_url:
                raise PicXError(
                    "source_video_url is required when mode='extend'",
                    status_code=400,
                )
        elif mode == "lipsync":
            if not source_video_url or not audio_url:
                raise PicXError(
                    "mode='lipsync' requires BOTH source_video_url and audio_url",
                    status_code=400,
                )
        elif mode == "edit":
            if not source_video_url or not image_url:
                raise PicXError(
                    "mode='edit' requires BOTH source_video_url and image_url",
                    status_code=400,
                )

        if not (1 <= duration <= 60):
            raise PicXError(
                f"duration must be 1-60 seconds (got {duration})", status_code=400
            )

        # ── Build request body ────────────────────────────────────────────────
        # Only the fields the chosen mode actually uses are forwarded, so a
        # stray URL left in from a prior call can't leak into a different mode.
        body: dict[str, Any] = {
            "mode": mode,
            "duration": duration,
            "resolution": resolution,
            "sound": sound,
        }
        if prompt_clean:
            body["prompt"] = prompt_clean
        if model:
            body["model"] = model
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if mode == "image":
            body["image_url"] = image_url
        elif mode == "reference":
            body["reference_urls"] = reference_urls
        elif mode == "frames":
            body["start_frame_url"] = start_frame_url
            if end_frame_url:
                body["end_frame_url"] = end_frame_url
        elif mode == "extend":
            body["source_video_url"] = source_video_url
        elif mode == "lipsync":
            body["source_video_url"] = source_video_url
            body["audio_url"] = audio_url
        elif mode == "edit":
            body["source_video_url"] = source_video_url
            body["image_url"] = image_url

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
    ) -> ToolResult:
        """Poll a generation by ID. Returns status, output_url, credits_used, error_message."""
        if not generation_id or not generation_id.strip():
            raise PicXError("generation_id is required", status_code=400)

        client = get_client()
        result = await client.get(f"/generations/{generation_id.strip()}")

        structured = {
            "id": result.get("id", generation_id),
            "status": result.get("status"),
            "output_url": result.get("output_url"),
            "credits_used": result.get("credits_used"),
            "error_message": result.get("error_message"),
        }

        status = structured["status"]
        output_url = structured["output_url"]
        content: list[Any] = [
            TextContent(
                type="text",
                text=f"Generation {structured['id']}: {status}"
                + (f" — {structured['error_message']}" if structured.get("error_message") else ""),
            )
        ]
        if status == "completed" and output_url:
            gen_type = result.get("type", "")
            mime, _ = mimetypes.guess_type(output_url)
            if not mime:
                mime = "video/mp4" if gen_type == "video" else "image/png"
            content.append(
                ResourceLink(
                    type="resource_link",
                    uri=output_url,
                    name=structured["id"],
                    title="Generated video" if gen_type == "video" else "Generated image",
                    mimeType=mime,
                )
            )
        return ToolResult(content=content, structured_content=structured)
