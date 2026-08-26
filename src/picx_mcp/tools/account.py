"""Account and usage tools.

Exports:
    register(mcp) — registers picx_get_account, picx_get_usage, picx_get_tier
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP

from ..client import PicXError
from ..context import get_client


def register(mcp: FastMCP) -> None:

    # ── picx_get_account ──────────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "Get the authenticated user's PicX account details including credit "
            "balance, email, and role. Use to check remaining credits before "
            "generation or to confirm account identity. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_get_account() -> dict[str, Any]:
        """Retrieve account info for the current API key holder.

        Returns a flat, model-friendly dict:
            {id, email, name, role, is_active, credits_balance,
             credits_total_earned, credits_total_used}
        """
        client = get_client()
        data = await client.get("/account/me")

        # Project a flat structure that's easy for models to reason about.
        credits: dict[str, Any] = data.get("credits", {})
        return {
            "id": data.get("id"),
            "email": data.get("email"),
            "name": data.get("name"),
            "role": data.get("role"),
            "is_active": data.get("is_active"),
            "credits_balance": credits.get("balance"),
            "credits_total_earned": credits.get("total_earned"),
            "credits_total_used": credits.get("total_used"),
        }

    # ── picx_get_usage ────────────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "Get API usage statistics for the authenticated account over a time "
            "period. Returns request counts, cost, credits used, and per-model "
            "breakdown. Use to report usage to the user or check spend. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_get_usage(
        period: Literal["7d", "30d", "90d"] | None = None,
    ) -> dict[str, Any]:
        """Get usage stats for the current API key holder.

        Args:
            period: Time window — "7d", "30d", or "90d". Defaults to API default
                    (typically 30d) when omitted.

        Returns:
            Flat dict: {total_requests, successful_requests, failed_requests,
                        total_cost_usd, credits_used, period_days, model_breakdown}
        """
        client = get_client()
        params: dict[str, Any] = {}
        if period:
            params["period"] = period
        return await client.get("/account/usage", params=params)

    # ── picx_get_tier ─────────────────────────────────────────────────────────

    @mcp.tool(
        description=(
            "Get the authenticated account's rate limits and daily credit cap. "
            "Use to understand throttling constraints before batch operations. "
            "Free — does not spend credits. "
            "NOTE: This endpoint may not be available on all API tiers; returns "
            "an error dict with status 404 if the endpoint does not exist."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_get_tier() -> dict[str, Any]:
        """Get rate-limit tier info for the current API key holder.

        Returns:
            Tier details from GET /v1/account/tier if the endpoint responds.
            On 404, returns {error: "endpoint not available", status: 404}.
        """
        client = get_client()
        try:
            return await client.get("/account/tier")
        except PicXError as exc:
            if exc.status_code == 404:
                return {"error": "endpoint not available", "status": 404}
            raise
