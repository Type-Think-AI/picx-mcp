"""PicX generation history + per-generation sub-resource tools.

Exposes:
    picx_list_generations         — history list (endpoint not yet live; 404-guarded)
    picx_get_generation_events    — bounded read of the SSE progress stream
    picx_get_generation_deliveries— webhook deliveries for one generation

🔴 GET /v1/generations (the LIST) DOES NOT EXIST YET — verified: returns 404 as
of 2026-08-26. picx_list_generations is implemented against the intended
contract and 404-guarded so it activates the moment the backend ships it. The
per-generation sub-resources (events, deliveries) ARE live.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..client import PicXError
from ..context import get_client, resolve_api_key
from ..settings import get_settings

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ── Tool registration ─────────────────────────────────────────────────────────

def register(mcp: "FastMCP") -> None:
    @mcp.tool(
        title="Generation History",
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
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
            client = await get_client()
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

    # ── picx_get_generation_events ────────────────────────────────────────────

    @mcp.tool(
        title="Get Generation Events",
        description=(
            "Read the progress-event stream for one generation (GET "
            "/v1/generations/{id}/events, a Server-Sent Events feed). MCP tools "
            "cannot hold a long-lived stream open, so this collects events into a "
            "list and RETURNS ONCE — as soon as a terminal event (status "
            "'completed'/'failed') arrives OR the timeout is reached, whichever "
            "comes first. It is a bounded snapshot of progress, not a live "
            "subscription: if the generation is still running when the timeout "
            "hits, call again to resume reading, or fall back to polling "
            "picx_get_generation. Returns {generation_id, events: [...], "
            "terminal: bool, count, timed_out: bool}. "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    )
    async def picx_get_generation_events(
        generation_id: str,
        timeout_seconds: float = 30.0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Collect SSE events for a generation until terminal or timeout.

        Args:
            generation_id: The generation to watch.
            timeout_seconds: Max wall-clock time to read the stream (1-120). The
                tool returns after this even if the generation is still running.
            max_events: Stop after collecting this many events (1-1000).
        """
        if not generation_id or not generation_id.strip():
            raise PicXError("generation_id is required", status_code=400)
        gen_id = generation_id.strip()
        timeout_seconds = max(1.0, min(120.0, timeout_seconds))
        max_events = max(1, min(1000, max_events))

        settings = get_settings()
        api_key = await resolve_api_key()
        url = f"{settings.picx_api_base}/generations/{gen_id}/events"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "User-Agent": "picx-mcp/0.1.0",
            "X-PicX-Source": "mcp",
        }

        events: list[dict[str, Any]] = []
        terminal = False
        timed_out = False
        deadline = time.monotonic() + timeout_seconds

        def _is_terminal(evt: dict[str, Any]) -> bool:
            status = str(evt.get("status") or evt.get("event") or "").lower()
            return status in {"completed", "failed", "succeeded", "error", "canceled", "cancelled"}

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds + 5)) as http:
                async with http.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise PicXError(
                            body.decode("utf-8", "replace")[:500] or f"HTTP {resp.status_code}",
                            status_code=resp.status_code,
                        )
                    data_buf: list[str] = []
                    async for line in resp.aiter_lines():
                        if time.monotonic() >= deadline:
                            timed_out = True
                            break
                        # SSE framing: "data:" lines accumulate; a blank line
                        # dispatches the event.
                        if line.startswith("data:"):
                            data_buf.append(line[5:].lstrip())
                        elif line == "":
                            if not data_buf:
                                continue
                            raw = "\n".join(data_buf)
                            data_buf = []
                            try:
                                evt = json.loads(raw)
                            except json.JSONDecodeError:
                                evt = {"raw": raw}
                            events.append(evt)
                            if _is_terminal(evt):
                                terminal = True
                                break
                            if len(events) >= max_events:
                                break
                    else:
                        # stream closed by server without a terminal marker
                        if data_buf:
                            raw = "\n".join(data_buf)
                            try:
                                events.append(json.loads(raw))
                            except json.JSONDecodeError:
                                events.append({"raw": raw})
                            if events and _is_terminal(events[-1]):
                                terminal = True
        except PicXError:
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError):
            timed_out = True
        except httpx.HTTPError as exc:
            raise PicXError(f"network error reading events for {gen_id}: {exc}", status_code=502) from exc

        return {
            "generation_id": gen_id,
            "events": events,
            "count": len(events),
            "terminal": terminal,
            "timed_out": timed_out,
        }

    # ── picx_get_generation_deliveries ────────────────────────────────────────

    @mcp.tool(
        title="Get Generation Deliveries",
        description=(
            "List the webhook deliveries that fired for one generation (GET "
            "/v1/generations/{id}/deliveries). Use to see whether the "
            "completed/failed webhook for a generation was delivered to the "
            "caller's endpoints and with what response. Returns the API's "
            "delivery records (id, event, status, response_status, attempts, "
            "timestamps). "
            "Free — does not spend credits."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    )
    async def picx_get_generation_deliveries(
        generation_id: str,
    ) -> Any:
        """List webhook deliveries for a single generation."""
        if not generation_id or not generation_id.strip():
            raise PicXError("generation_id is required", status_code=400)
        client = await get_client()
        return await client.get(f"/generations/{generation_id.strip()}/deliveries")
