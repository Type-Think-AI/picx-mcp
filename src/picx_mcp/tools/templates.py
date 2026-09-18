"""PicX template catalogue tools — the headline discovery feature.

The catalogue holds ~50,000 curated prompts: proven starting points that
outperform anything an agent would invent from scratch. The intended workflow
is: search the catalogue → pick a template → feed its `prompt` straight into
picx_generate_image or picx_generate_video.

## Three server behaviours these tools honour (and document to the caller)

1. `total` is an ESTIMATE, not an exact count. The catalogue is ~50k rows and a
   COUNT over it was deliberately avoided, so the server returns
   `offset + len(page) + 1` when a further page exists. NEVER present `total` as
   an exact number. To exhaust results, page (increase `offset` by `limit`)
   until a page comes back SHORTER than `limit`.

2. The `topic` FILTER works, but the `topic` FIELD on every returned row is
   always `null`. Topic is a query-time keyword bucket, not a stored per-row
   column — there is nothing to populate the field with. Filter by it; don't
   read it back.

3. Premium/gated templates return `prompt: null` BY DESIGN (public redaction).
   A null prompt means the template is gated, not that data is missing. Such a
   template can still be surfaced (title, preview, tags) but its prompt cannot
   be fed into a generation through an API key.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ..client import PicXError
from ..context import get_client

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ── Response cache ────────────────────────────────────────────────────────────
# The catalogue is hot, shared and safely stale — a short cache saves redundant
# calls when an agent iterates on search terms within one session.
_CACHE_TTL = 300  # seconds
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# ── Tool registration ─────────────────────────────────────────────────────────


def register(mcp: "FastMCP") -> None:
    @mcp.tool(
        description=(
            "Search PicX's catalogue of ~50,000 curated generation templates "
            "(GET /v1/templates). USE THIS FIRST when a user wants to generate an "
            "image or video in a recognisable style — a proven template prompt "
            "beats an invented one. The workflow is: search here, pick a template, "
            "then pass its `prompt` field to picx_generate_image or "
            "picx_generate_video.\n"
            "\n"
            "Filters (all optional): q (free-text keywords), media_type "
            "('image'|'video'), topic (keyword bucket, see caveat below), tags "
            "(repeatable, e.g. ['cinematic','portrait']), target_model (only "
            "templates built for that model), featured (editor picks), trending "
            "(popular now). Pagination: limit (1-100, default 30) and offset "
            "(>=0, default 0).\n"
            "\n"
            "Returns {templates: [TemplateInfo], total, limit, offset}. Each "
            "TemplateInfo = {id, title, prompt (str|null), media_type, topic "
            "(always null), tags[], target_model, preview_url, thumbnail_url, "
            "is_featured, likes}.\n"
            "\n"
            "THREE THINGS TO KNOW:\n"
            "  • `total` is an ESTIMATE, not an exact count (the catalogue is too "
            "large to count). Never quote it as exact. To page through all "
            "results, keep increasing `offset` by `limit` until you get a page "
            "with fewer than `limit` rows — that's the last page.\n"
            "  • The `topic` FILTER works, but the `topic` FIELD on each row is "
            "ALWAYS null. Don't read topic back from a result; only use it to "
            "filter.\n"
            "  • A template with `prompt: null` is PREMIUM/GATED (the prompt is "
            "redacted for public keys), not broken. You can show it but cannot "
            "feed its prompt into a generation.\n"
            "Free — does not spend credits."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def picx_search_templates(
        q: str | None = None,
        media_type: str | None = None,
        topic: str | None = None,
        tags: list[str] | None = None,
        target_model: str | None = None,
        featured: bool | None = None,
        trending: bool | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search the PicX template catalogue.

        Args:
            q: Free-text keyword search.
            media_type: "image" or "video".
            topic: Query-time keyword bucket to filter by (the field on each row
                is always null — filter only).
            tags: Repeatable tag filter, e.g. ["cinematic", "4k"].
            target_model: Only templates built for this model id.
            featured: If True, only editorially featured templates.
            trending: If True, only currently trending templates.
            limit: Results per page, 1-100. Default 30.
            offset: 0-based pagination offset. Default 0.

        Returns:
            {templates: [TemplateInfo], total: int (ESTIMATE), limit, offset}.
        """
        if media_type is not None and media_type not in ("image", "video"):
            raise PicXError(
                f"media_type must be 'image' or 'video' (got {media_type!r})",
                status_code=400,
            )
        limit = max(1, min(100, limit))
        offset = max(0, offset)

        params: dict[str, Any] = {
            "q": q,
            "media_type": media_type,
            "topic": topic,
            "target_model": target_model,
            "limit": limit,
            "offset": offset,
        }
        # Booleans: only forward when explicitly set. httpx serialises
        # True/False to the strings "true"/"false", which FastAPI parses
        # correctly — so featured=False is sent as "false", NOT dropped.
        if featured is not None:
            params["featured"] = featured
        if trending is not None:
            params["trending"] = trending
        # Repeatable tags: httpx expands a list into ?tags=a&tags=b.
        if tags:
            params["tags"] = tags

        cache_key = f"search:{sorted((k, str(v)) for k, v in params.items() if v is not None)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        client = get_client()
        result = await client.get("/templates", params=params)
        _cache_set(cache_key, result)
        return result

    @mcp.tool(
        description=(
            "Fetch one template by its id (GET /v1/templates/{template_id}). Use "
            "after picx_search_templates to inspect a specific template before "
            "generating from it. Returns a TemplateInfo = {id, title, prompt "
            "(str|null — null means premium/gated, not missing), media_type, "
            "topic (always null), tags[], target_model, preview_url, "
            "thumbnail_url, is_featured, likes}. Returns a 404 error if the "
            "template is not live/approved. "
            "Free — does not spend credits."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def picx_get_template(
        template_id: str,
    ) -> dict[str, Any]:
        """Fetch a single template by id.

        Args:
            template_id: The template's id (from picx_search_templates).

        Returns:
            A TemplateInfo dict. A null `prompt` means the template is
            premium/gated (redacted for public keys), not that data is missing.
        """
        tid = str(template_id).strip()
        if not tid:
            raise PicXError("template_id is required", status_code=400)

        cache_key = f"get:{tid}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        client = get_client()
        result = await client.get(f"/templates/{tid}")
        _cache_set(cache_key, result)
        return result
