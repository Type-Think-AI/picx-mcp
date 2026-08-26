"""FastMCP 4 server entry point.

Exports:
    build_server() -> FastMCP   — construct and wire the server instance
    build_app()                 — ASGI app for `uvicorn picx_mcp.server:app`
    app                         — module-level ASGI app (uvicorn target)
"""

from __future__ import annotations

import sys

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .settings import get_settings
from .tools import register_all


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_server() -> FastMCP:
    """Construct the FastMCP instance with all tools registered."""
    settings = get_settings()

    # ── RequestStateSecurity ──────────────────────────────────────────────────
    # Protects state tokens carried between rounds of interactive (task) tool
    # calls. MUST be the same key on every replica — otherwise a round started
    # on replica A and resumed on replica B will reject the state token.
    request_state_kwargs: dict = {}
    if settings.request_state_key:
        from mcp.server.request_state import RequestStateSecurity

        request_state_kwargs["request_state_security"] = RequestStateSecurity(
            keys=[settings.request_state_key.encode()]
        )
    else:
        _log_stderr(
            "WARNING: request_state_key not set. Multi-replica interactive rounds "
            "will fail — state tokens cannot be validated across replicas."
        )

    mcp = FastMCP("PicX Studio", **request_state_kwargs)

    # ── Tools ─────────────────────────────────────────────────────────────────
    registered = register_all(mcp)
    _log_stderr(f"Registered tool modules: {registered}")

    # ── TasksExtension (optional) ─────────────────────────────────────────────
    # Default backend is in-memory single-process. MUST be pointed at Valkey/Redis
    # before running >1 replica — otherwise task state is partitioned and will 404
    # on any replica that didn't start the task.
    try:
        from fastmcp_tasks import TasksExtension  # type: ignore[import-untyped]

        mcp.add_extension(TasksExtension())
        _log_stderr("TasksExtension loaded (in-memory backend — single replica only)")
    except ImportError:
        _log_stderr("fastmcp-tasks not installed; TasksExtension unavailable")

    # ── Health endpoint ───────────────────────────────────────────────────────
    # Custom routes are NOT behind auth middleware (by design, for LB probes).
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "healthy", "service": "picx-mcp", "tools": len(registered)}
        )

    return mcp


def build_app():
    """Return the ASGI app suitable for uvicorn / gunicorn.

    stateless_http=True is MANDATORY. FastMCP docs:
        MCP clients including Cursor and Claude Code use fetch() internally and
        do not forward Set-Cookie, so sticky sessions CANNOT work — stateless
        mode or single instance, no third option.
    """
    settings = get_settings()
    mcp = build_server()
    return mcp.http_app(
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=settings.allowed_hosts,
        path="/mcp",
    )


# Module-level ASGI app so `uvicorn picx_mcp.server:app` works out of the box.
app = build_app()
