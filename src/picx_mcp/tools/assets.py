"""Asset management tools — upload, list, delete.

The upload tool is foundational: `/v1/images/edit` rejects data URIs, so every
local file must become a permanent CDN URL via this tool before it can be used
as an edit source or video frame.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ..client import PicXError
from ..context import get_client


# ── MIME detection ────────────────────────────────────────────────────────────

_ALLOWED_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def _detect_mime(path: str) -> str:
    """Return MIME type from file extension, restricted to supported formats."""
    ext = os.path.splitext(path)[1].lower()
    mime = _ALLOWED_MIMES.get(ext)
    if mime is None:
        supported = ", ".join(sorted(_ALLOWED_MIMES.keys()))
        raise PicXError(
            f"Unsupported file extension '{ext}'. Supported: {supported}",
            status_code=400,
        )
    return mime


# ── Tool definitions ──────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    """Register asset management tools on the FastMCP instance."""

    @mcp.tool(
        description=(
            "Upload a local file to PicX and get a permanent CDN URL. "
            "THIS IS REQUIRED before calling picx_edit_image or using a local file as a "
            "video frame — /v1/images/edit rejects data URIs and local paths; only https "
            "URLs are accepted. If the input is already an https:// URL, it is returned "
            "unchanged (no upload). "
            "Supported formats: PNG, JPEG, WebP, GIF, MP4, MOV. "
            "Requires the `uploads:write` scope on your API key."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def picx_upload_asset(
        path_or_url: Annotated[
            str,
            Field(
                description=(
                    "Absolute path to a local file (e.g. /tmp/photo.png) "
                    "OR an existing https:// URL. If already https, returned as-is."
                )
            ),
        ],
    ) -> str:
        """Upload a local file to PicX CDN, or pass through an existing URL."""
        # Pass-through: already a remote URL
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url

        # Local file: validate, read, detect MIME, upload
        path = os.path.expanduser(path_or_url)
        if not os.path.isabs(path):
            raise PicXError(
                f"Path must be absolute, got relative path: {path_or_url!r}",
                status_code=400,
            )
        if not os.path.isfile(path):
            raise PicXError(
                f"File not found or not readable: {path!r}",
                status_code=400,
            )

        mime = _detect_mime(path)
        filename = os.path.basename(path)

        content = await _read_file(path)

        client = get_client()
        result = await client.upload(content=content, filename=filename, mime=mime)

        # API returns {"url": "https://cdn.picxstudio.com/...", "id": "..."}
        url = result.get("url") if isinstance(result, dict) else None
        if not url:
            raise PicXError(
                "Upload succeeded but response did not contain a URL.",
                status_code=502,
                payload=result,
            )
        return url

    @mcp.tool(
        description=(
            "List uploaded assets with offset-based pagination. "
            "Returns {assets: [...], total, limit, offset}. "
            "Each asset has id, url, filename, mime_type, created_at."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def picx_list_assets(
        limit: Annotated[
            int,
            Field(ge=1, le=100, default=20, description="Number of assets to return (1-100)."),
        ] = 20,
        offset: Annotated[
            int,
            Field(ge=0, default=0, description="Offset for pagination (0-based)."),
        ] = 0,
    ) -> dict:
        """List uploaded assets with offset pagination."""
        client = get_client()
        result = await client.get("/assets", params={"limit": limit, "offset": offset})
        return result

    @mcp.tool(
        description=(
            "Permanently delete an uploaded asset by ID. "
            "The asset's CDN URL will stop resolving. This cannot be undone."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def picx_delete_asset(
        asset_id: Annotated[
            str,
            Field(description="The asset ID to delete (from picx_list_assets)."),
        ],
    ) -> str:
        """Delete an asset by ID."""
        client = get_client()
        await client.delete(f"/assets/{asset_id}")
        return f"Asset {asset_id} deleted."


# ── Async file read helper ────────────────────────────────────────────────────


async def _read_file(path: str) -> bytes:
    """Read a file asynchronously.

    Uses anyio (already a transitive dep of FastMCP/Starlette) to avoid blocking
    the event loop on large files.
    """
    import anyio

    return await anyio.Path(path).read_bytes()
