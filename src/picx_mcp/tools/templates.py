"""PicX template catalogue tools.

The catalogue holds 50,000+ curated prompts — proven starting points that
outperform anything an agent would invent from scratch. Two tools let a model
search and inspect templates before deciding whether to generate from one.

Note: GET /templates/{id}/prompt (the FULL prompt body for premium templates)
requires session auth and returns 'Invalid API key' when tested with a pxsk_
key. It is deliberately NOT exposed here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from ..settings import get_settings

if TYPE_CHECKING:
    from fastmcp import FastMCP

# ── Response cache ────────────────────────────────────────────────────────────
# The template catalogue is hot, shared, and safely stale — a 5-minute cache
# saves hundreds of redundant calls when an agent iterates on search terms.
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _templates_base_url() -> str:
    """Templates live at the API root, NOT under /v1.

    The PicX template routes are root-mounted at /templates/ and are fully
    public (no auth required). We derive the root from the configured base_url
    by stripping the /v1 suffix.
    """
    settings = get_settings()
    return settings.picx_api_base.replace("/v1", "")


async def _get_templates(
    path: str, params: dict[str, Any] | None = None
) -> Any:
    """GET against the public /templates/ surface."""
    url = f"{_templates_base_url()}{path}"
    query = {k: v for k, v in (params or {}).items() if v is not None}
    settings = get_settings()

    cache_key = f"{url}?{sorted(query.items())}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=settings.picx_api_timeout) as http:
        resp = await http.get(
            url,
            params=query,
            headers={"User-Agent": "picx-mcp/0.1.0"},
        )

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:500])
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f"Templates API error ({resp.status_code}): {detail}")

    data = resp.json()
    _cache_set(cache_key, data)
    return data


# ── Tool registration ─────────────────────────────────────────────────────────

def register(mcp: "FastMCP") -> None:
    @mcp.tool(
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_search_templates(
        search: str | None = None,
        category: str | None = None,
        media_type: str | None = None,
        target_model: str | None = None,
        is_featured: bool | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search the PicX template catalogue (50,000+ curated prompts).

        Use this to find PROVEN generation prompts instead of inventing one.
        Templates include sample_prompt, category, tags, and media_type — pick
        one and pass its prompt to picx_generate_image or picx_generate_video.

        Params:
            search: Free-text keyword search across template names/descriptions.
            category: Filter by category slug (e.g. "portraits", "landscapes").
            media_type: "image", "video", or "audio".
            target_model: Filter by compatible model (e.g. "flux-1.1-pro").
            is_featured: If True, return only editorially featured templates.
            tags: Filter by tags (AND logic). E.g. ["cinematic", "4k"].
            limit: Results per page, 1-100. Default 10.
            page: Page number. Default 1.

        Returns {templates: [...], total: int}. Each template has id, name,
        sample_prompt, description, category, media_type, tags, and more.

        Free — no credits consumed.
        """
        limit = max(1, min(100, limit))
        page = max(1, page)

        params: dict[str, Any] = {
            "search": search,
            "category": category,
            "media_type": media_type,
            "target_model": target_model,
            "is_featured": is_featured,
            "limit": limit,
            "page": page,
        }
        if tags:
            params["tags"] = tags

        return await _get_templates("/templates/", params=params)

    @mcp.tool(
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_get_template(
        template_id: int,
    ) -> dict[str, Any]:
        """Get full details of a single PicX template by ID.

        Returns the template's metadata including name, sample_prompt,
        description, category, media_type, tags, and configuration. Use after
        searching to inspect a specific template before generating from it.

        Free — no credits consumed.
        """
        return await _get_templates(f"/templates/{template_id}")
