"""Model catalogue tools.

Exports:
    register(mcp)                                 — registers picx_list_models
    estimate_credits(client, model, size) -> int  — live credit lookup (no cache)
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastmcp import FastMCP

from ..client import PicXClient
from ..context import get_client

# ── In-process cache ──────────────────────────────────────────────────────────
# 5-minute TTL. Good enough for a single replica; multi-replica deployments
# should migrate to a Redis-backed KeyValueResponseCacheStore so every replica
# shares one warm catalogue.
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # seconds


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


async def _fetch_models(client: PicXClient, type_filter: str | None = None) -> dict[str, Any]:
    """GET /v1/models with optional type filter. Uses in-process cache."""
    cache_key = f"models:{type_filter or 'all'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params: dict[str, Any] = {}
    if type_filter:
        params["type"] = type_filter

    result = await client.get("/models", params=params)
    _cache_set(cache_key, result)
    return result


# ── Exported helper ───────────────────────────────────────────────────────────


async def estimate_credits(client: PicXClient, model: str, size: str) -> int | None:
    """Look up the credit cost for (model, size) from the live catalogue.

    Returns None when the model or size is not found. NEVER hardcodes costs —
    always reads from the API. Callers should surface "unknown cost" to the user
    rather than guessing.
    """
    data = await _fetch_models(client)
    models_list: list[dict[str, Any]] = data.get("models", [])
    for m in models_list:
        if m.get("id") == model:
            credits_map: dict[str, int] = m.get("credits", {})
            return credits_map.get(size)
    return None


# ── Tool registration ─────────────────────────────────────────────────────────


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "List available PicX image and video generation models with their credit "
            "costs per output size. Use this to discover model IDs before calling "
            "generation tools, and to show users accurate pricing. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_list_models(
        type: Literal["image", "video"] | None = None,
    ) -> dict[str, Any]:
        """List available models, optionally filtered by type.

        Args:
            type: Filter to "image" or "video" models only. Omit for all.

        Returns:
            {models: [{id, name, type, credits: {"1K": 35, "2K": 53, ...}}]}
        """
        client = get_client()
        return await _fetch_models(client, type_filter=type)
