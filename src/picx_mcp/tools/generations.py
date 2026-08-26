"""PicX generation history tools.

🔴 GET /v1/generations DOES NOT EXIST YET — verified: returns 404 as of
2026-08-26. This module is implemented against the intended contract so it is
ready to activate the moment the backend ships the endpoint. The handler
catches the 404 and returns an honest message rather than surfacing a raw error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..client import PicXError
from ..context import get_client

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ── Tool registration ─────────────────────────────────────────────────────────

def register(mcp: "FastMCP") -> None:
    @mcp.tool(
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_list_generations(
        type: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List your recent image/video generations (history).

        Params:
            type: Filter by "image" or "video". Omit for all.
            status: Filter by status string (e.g. "completed", "failed").
            limit: Number of results, 1-50. Default 20.

        Returns a list of generation records with id, type, status, prompt,
        output_url, model, created_at, and credit cost.

        Free — no credits consumed. Requires authentication.
        """
        # 🔴 This endpoint is not yet live on the PicX API. Once shipped,
        # remove the try/except 404 guard and let errors propagate normally.
        limit = max(1, min(50, limit))

        params: dict[str, Any] = {
            "type": type,
            "status": status,
            "limit": limit,
        }

        try:
            client = get_client()
            return await client.get("/generations", params=params)
        except PicXError as exc:
            if exc.status_code == 404:
                return {
                    "generations": [],
                    "total": 0,
                    "_notice": (
                        "Generation history is not yet available on this API version. "
                        "The /v1/generations endpoint has not shipped yet — this tool "
                        "will activate automatically when it does."
                    ),
                }
            raise
