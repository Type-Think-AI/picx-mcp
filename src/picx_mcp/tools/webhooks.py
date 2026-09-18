"""Webhook delivery tools — the API-key-authorized subset of the webhook surface.

## Scope: deliveries only, deliberately

Webhook endpoint CRUD (create / list / update / delete / test) is a
session-authenticated `/api` operation, NOT part of the public `/v1` surface a
`pxsk_` key can reach. Exposing those here would hand an agent tools that
silently 404 or 401. So this module exposes ONLY the two delivery routes that
are genuinely available to an API key:

    GET  /v1/webhooks/{webhook_id}/deliveries        — inspect a webhook's history
    POST /v1/webhooks/deliveries/{delivery_id}/redeliver — retry a failed delivery

A generation-scoped delivery view (GET /v1/generations/{id}/deliveries) lives in
generations.py — it answers "did the webhook for THIS render fire?" rather than
"what has this endpoint received?".
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
        description=(
            "List the delivery attempts for one webhook endpoint (GET "
            "/v1/webhooks/{webhook_id}/deliveries). Use to inspect what events a "
            "webhook has received and whether each delivery succeeded — each "
            "record carries id, event, status, response_status, attempts and "
            "timestamps. To find the delivery_id you need for a redelivery, list "
            "here first. "
            "NOTE: creating/editing/deleting webhook endpoints is a "
            "session-authenticated dashboard operation and is intentionally NOT "
            "exposed to API keys — only reading deliveries and redelivering are. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def picx_get_webhook_deliveries(
        webhook_id: str,
    ) -> Any:
        """List deliveries for a single webhook endpoint by ID."""
        if not webhook_id or not webhook_id.strip():
            raise PicXError("webhook_id is required", status_code=400)
        client = get_client()
        return await client.get(f"/webhooks/{webhook_id.strip()}/deliveries")

    @mcp.tool(
        description=(
            "Re-send a webhook delivery that previously failed (POST "
            "/v1/webhooks/deliveries/{delivery_id}/redeliver). Use after finding "
            "a failed delivery via picx_get_webhook_deliveries or "
            "picx_get_generation_deliveries. This re-fires the SAME signed payload "
            "to the endpoint; it does NOT regenerate anything and does NOT cost "
            "credits. Returns the new delivery attempt record. "
            "Only call this on an explicit request to retry a delivery — it "
            "triggers a live outbound HTTP call to the customer's endpoint."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def picx_redeliver_webhook(
        delivery_id: str,
    ) -> Any:
        """Redeliver a single webhook delivery by ID."""
        if not delivery_id or not delivery_id.strip():
            raise PicXError("delivery_id is required", status_code=400)
        client = get_client()
        return await client.post(
            f"/webhooks/deliveries/{delivery_id.strip()}/redeliver"
        )
