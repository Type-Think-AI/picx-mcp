"""Account and usage tools.

Exports:
    register(mcp) — registers picx_get_account, picx_get_usage, picx_get_tier,
                    picx_get_profile
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from ..client import PicXError
from ..context import _verified_access_token, get_client

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:

    # ── picx_get_account ──────────────────────────────────────────────────────

    @mcp.tool(
        title="Account & Credit Balance",
        description=(
            "Get the authenticated user's PicX account details including credit "
            "balance, email, and role. Use to check remaining credits before "
            "generation or to confirm account identity. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    )
    async def picx_get_account() -> dict[str, Any]:
        """Retrieve account info for the current API key holder.

        Returns a flat, model-friendly dict:
            {id, email, name, role, is_active, credits_balance,
             credits_total_earned, credits_total_used}
        """
        client = await get_client()
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
        title="Usage & Spend History",
        description=(
            "Get API usage statistics for the authenticated account over a time "
            "period. Returns request counts, cost, credits used, and per-model "
            "breakdown. Use to report usage to the user or check spend. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
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
        client = await get_client()
        params: dict[str, Any] = {}
        if period:
            params["period"] = period
        return await client.get("/account/usage", params=params)

    # ── picx_get_tier ─────────────────────────────────────────────────────────

    @mcp.tool(
        title="Rate Limits & Daily Cap",
        description=(
            "Get the authenticated account's rate limits and daily credit cap. "
            "Use to understand throttling constraints before batch operations. "
            "Free — does not spend credits. "
            "NOTE: This endpoint may not be available on all API tiers; returns "
            "an error dict with status 404 if the endpoint does not exist."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    )
    async def picx_get_tier() -> dict[str, Any]:
        """Get rate-limit tier info for the current API key holder.

        Returns:
            Tier details from GET /v1/account/tier if the endpoint responds.
            On 404, returns {error: "endpoint not available", status: 404}.
        """
        client = await get_client()
        try:
            return await client.get("/account/tier")
        except PicXError as exc:
            if exc.status_code == 404:
                return {"error": "endpoint not available", "status": 404}
            raise

    # ── picx_get_profile ──────────────────────────────────────────────────────
    #
    # WHY IT LIVES HERE: this is an identity/account read. It resolves the same
    # caller `picx_get_account` does and, when it enriches the profile, hits the
    # same `/account/me` endpoint through the same `get_client()` path. Keeping
    # it beside the other account read tools puts the two identity surfaces in
    # one file rather than inventing a module for a single tool. `account` is
    # already in tools.MODULES, so no registration list changes.
    #
    # WHY A SEPARATE TOOL FROM picx_get_account: this is OpenAI's Apps "profile"
    # contract, not a general account read. Its output schema is fixed by OpenAI
    # (top-level id/name/email/nickname, required:["id"], additionalProperties
    # False) and its identity rules differ (`id` must be the OPAQUE stable
    # subject, never the email). Folding it into picx_get_account would change
    # that tool's published output schema — which is exactly what the task
    # forbids for existing tools.

    @mcp.tool(
        title="Account Profile (OpenAI Apps identity)",
        description=(
            "Return a compact, stable identity profile for the currently linked "
            "PicX account: an opaque account id and, when available, name and "
            "email. Used by the host to tell multiple linked accounts apart. "
            "Free — does not spend credits. The `id` is an opaque account "
            "identifier, NOT the email; email is for display only."
        ),
        # `meta` is MERGED (not replaced) into the wire `_meta`: FastMCP's
        # get_meta() unions this dict with its own `fastmcp` block, so the
        # descriptor carries BOTH `openai/profile: true` and `fastmcp.tags`.
        # Verified empirically against fastmcp 4.0.0b3, and asserted on the wire
        # in tests/test_openai_apps_profile_tool.py.
        meta={"openai/profile": True},
        # The output schema is OpenAI's fixed profile shape: fields at TOP level,
        # `id` required, additionalProperties disallowed. Declared explicitly
        # because a `-> ToolResult` return annotation infers no schema (null),
        # and OpenAI needs the shape published. FastMCP validates the returned
        # structured_content against this, so a drifting profile object fails
        # loudly rather than shipping a malformed identity.
        output_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Opaque, stable account identifier. NOT the email.",
                },
                "name": {"type": "string", "description": "Display name, if known."},
                "email": {
                    "type": "string",
                    "description": "Email for display only; not the identity.",
                },
                "nickname": {"type": "string", "description": "Short handle, if known."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    )
    async def picx_get_profile() -> ToolResult:
        """Identity profile for the linked account (OpenAI Apps `openai/profile`).

        Returns a single JSON object with the profile at TOP level
        ({id, name?, email?, nickname?}) in BOTH `structured_content` and a JSON
        text content block, as OpenAI's Apps auth guide asks for.

        Identity rules (from OpenAI's guide):
          - `id` MUST be stable and opaque and MUST NOT be the email.
          - We use the verified OAuth `sub`, which IS the PicX `User.id` UUID
            (see context._verified_access_token / access_token.subject). So the
            required field needs NO network call.

        Degradation, in order of preference for `id`:
          1. OAuth configured: `id` = verified token `subject`. name/email are
             best-effort from `/account/me`; if that call fails we still return
             an id-only profile — a profile tool that ERRORS is worse than a
             sparse one.
          2. OAuth NOT configured (`pxsk_` passthrough — every deployment
             today): there is no verified subject at all, so `id` falls back to
             the account id from `/account/me`. This keeps the tool usable in
             passthrough mode instead of being permanently inert.
          3. Neither yields an id: raise. `required:["id"]` cannot be honoured
             without one, and a profile object missing its identity field is not
             a valid profile.
        """
        access_token = _verified_access_token()
        subject = getattr(access_token, "subject", None) if access_token else None

        profile: dict[str, Any] = {}

        # Enrich (and, in passthrough mode, source the id) from /account/me.
        # Best-effort: any failure degrades rather than erroring the tool.
        account: dict[str, Any] = {}
        try:
            client = await get_client()
            account = await client.get("/account/me")
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never error
            logger.info("picx_get_profile: /account/me unavailable, degrading (%r)", exc)

        # `id`: prefer the opaque verified subject; fall back to the account id
        # only when there is no OAuth subject (passthrough deployments).
        profile["id"] = subject or account.get("id")

        # name/email are display-only enrichment. Include only when present and
        # truthy so the object never carries null fields (additionalProperties
        # is False and these keys are optional, not required-with-null).
        name = account.get("name")
        if name:
            profile["name"] = name
        email = account.get("email")
        if email:
            profile["email"] = email

        if not profile.get("id"):
            # No opaque subject and no account id — cannot satisfy required:["id"].
            raise PicXError(
                "No account identity available to build a profile. Authenticate "
                "with an OAuth-linked account or a PicX API key.",
                status_code=401,
            )

        # Return the SAME object twice: as structured_content (machine-readable,
        # validated against the output schema) AND serialized as JSON in a text
        # content block, per OpenAI's Apps guide. Returning a ToolResult with
        # structured_content + content (and no meta / not is_error) makes
        # to_mcp_result() emit the (content, structured_content) tuple, so both
        # reach the wire. Text is compact JSON (sorted keys) for stable output.
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(profile, sort_keys=True, separators=(",", ":")),
                )
            ],
            structured_content=profile,
        )
