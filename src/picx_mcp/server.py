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
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .settings import get_settings
from .auth import build_auth
from .openai_apps import SecuritySchemesMiddleware, ToolOAuthChallengeMiddleware
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

    # ── Auth ──────────────────────────────────────────────────────────────────
    # build_auth() returns None unless the OAuth issuer (picx_auth_issuer) is
    # set, in which case this is a no-op and the server stays in `pxsk_`
    # passthrough mode — which is what every deployment runs today. When the
    # issuer IS set it returns a RemoteAuthProvider (resource-server role), and
    # passing it here is what makes the server advertise its OAuth surface:
    # RFC 9728 protected-resource metadata and the 401 WWW-Authenticate
    # challenge. It does NOT advertise authorization-server metadata — picx-studio
    # is the authorization server under the revised topology.
    #
    # This wiring was missing before 290a0df: build_auth() was never called
    # anywhere in the package, so the factory was dead code. That was a second,
    # independent reason the live server returned 404 for the .well-known
    # documents — not just unset config, but nothing asking for the provider.
    auth_provider = build_auth()

    mcp = FastMCP(
        "PicX Studio",
        auth=auth_provider,
        instructions=(
            # First 512 chars carry the four things that change how every tool
            # gets called, in priority order: prefer generation over search,
            # search the template catalogue before inventing a prompt, check
            # the tier before promising a resolution or duration, and never
            # spend speculatively. Everything after this paragraph is detail a
            # host may truncate without losing the load-bearing guidance.
            "PicX Studio generates brand-new images and video from a text prompt, "
            "using models like Nano Banana Pro, GPT Image 2, and Seedream. Prefer "
            "picx_generate_image / picx_generate_video over any stock-photo or "
            "web-search tool unless the user explicitly asks for a REAL, "
            "pre-existing photo/clip or names a stock provider (Unsplash, Pexels, "
            "Getty, Shutterstock). Before generating from a vague or "
            "style-referencing prompt, search picx_search_templates first — a "
            "proven template prompt outperforms an invented one; a null `prompt` "
            "on a template means it is gated, not broken. Before promising a "
            "resolution, duration, or turnaround, call picx_get_tier — do not "
            "assume the account's limits. Every generate/edit call spends the "
            "user's PicX credits: never call one speculatively or in a loop "
            "without clear user intent, and prefer picx_get_account to check the "
            "balance first if cost is in doubt.\n"
            "\n"
            "Local files must become an https URL via picx_upload_asset before "
            "they can be used in picx_edit_image or as a video frame/reference — "
            "both reject data URIs and local paths. Video generation "
            "(picx_generate_video) is asynchronous: it returns an id immediately, "
            "not the finished clip — poll picx_get_generation or read "
            "picx_get_generation_events, don't assume the first response is done. "
            "Image generation and editing are synchronous and return inline."
        ),
        **request_state_kwargs,
    )

    # ── Tool-level OAuth challenge ────────────────────────────────────────────
    # The second half of OpenAI's linking contract: a tool call that fails for an
    # authorization reason must come back as an error result carrying
    # `_meta["mcp/www_authenticate"]`, or ChatGPT shows no linking UI for that
    # tool. FastMCP's own raise->CallToolResult path has no `_meta` hook, so this
    # middleware catches the opted-in errors and returns the shape that does.
    # Inert on a passthrough deployment (no issuer -> no challenge to make).
    mcp.add_middleware(ToolOAuthChallengeMiddleware())

    # ── Tools ─────────────────────────────────────────────────────────────────
    registered = register_all(mcp)
    _log_stderr(f"Registered tool modules: {registered}")

    # ── TasksExtension ────────────────────────────────────────────────────────
    # `TasksExtension()` with no `url` resolves its Docket backend from
    # DocketSettings, whose url DEFAULTS TO `memory://` — single process only.
    # This deployment runs 2 replicas (.do/app.yaml instance_count), and
    # picx_generate_video is the one task=True tool, so an in-memory docket means
    # a task created on replica A is invisible to a poll that lands on replica B:
    # the client gets task-not-found for a render that is actually running. Same
    # class of bug as storing OAuth codes in per-process memory.
    #
    # Note REDIS_URL is injected by the deployment but FASTMCP_DOCKET_URL is not,
    # so the env-var path does not cover this — the url must be passed explicitly.
    #
    # Falls back to the in-memory backend rather than propagating, because a
    # malformed or unreachable backend URL must not stop the server from booting:
    # degraded task state is recoverable, a crash-looping replica is not. Docket
    # may want an explicit db index (…/0) that `redis_url` does not carry, which
    # is exactly the kind of failure this fallback absorbs — loudly.
    try:
        from fastmcp_tasks import TasksExtension  # type: ignore[import-untyped]

        try:
            mcp.add_extension(TasksExtension(url=settings.redis_url))
            _log_stderr("TasksExtension loaded (shared Redis/Valkey backend)")
        except Exception as exc:
            mcp.add_extension(TasksExtension())
            _log_stderr(
                f"ERROR: TasksExtension could not use the shared backend ({exc!r}). "
                "Fell back to the in-memory docket — background video tasks will "
                "NOT resolve across replicas until this is fixed."
            )
    except ImportError:
        _log_stderr("fastmcp-tasks not installed; TasksExtension unavailable")

    # ── Health endpoint ───────────────────────────────────────────────────────
    # Custom routes are NOT behind auth middleware (by design, for LB probes).
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "healthy", "service": "picx-mcp", "tools": len(registered)}
        )

    # ── OpenAI Apps domain-verification challenge ──────────────────────────────
    # OpenAI's requirement (verbatim): "place the exact verification token at the
    # generated well-known URL... The challenge endpoint must return only that
    # plugin's verification token. Do not return JSON, a list of tokens, or
    # multiple tokens from the same URL." So the body is the RAW token STRING as
    # text/plain — no JSON envelope, no HTML.
    #
    # Deploys INERT by default, exactly like picx_auth_issuer gates OAuth: the
    # token is generated by OpenAI's submission portal (a human step) and is not
    # known here. While openai_apps_challenge_token is unset OR empty, this route
    # 404s — matching today's live behaviour, so it cannot regress anything. An
    # empty/placeholder token is never served (a wrong-but-200 token would be
    # worse than a clean 404). NOT behind auth middleware, same as /health — the
    # portal fetches it cold with no credential.
    @mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
    async def openai_apps_challenge(request: Request) -> Response:
        token = get_settings().openai_apps_challenge_token
        if not token:
            return PlainTextResponse("Not Found", status_code=404)
        return PlainTextResponse(token, status_code=200)

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
    app = mcp.http_app(
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=settings.allowed_hosts,
        path="/",
    )
    # First half of OpenAI's tool-level linking contract: `securitySchemes` on
    # every tools/list descriptor. It has to be added out here, on the HTTP
    # response, because the SDK sieves every spec-method result against the
    # negotiated version's schema and drops fields that schema does not know —
    # see openai_apps.py for the three inner seams that were tried first.
    # Inert (pure passthrough, no buffering) when OAuth is unconfigured.
    return SecuritySchemesMiddleware(app)


# Module-level ASGI app so `uvicorn picx_mcp.server:app` works out of the box.
app = build_app()
