"""Tool-level OAuth signalling for OpenAI Apps (ChatGPT / Codex).

## Why this module exists

OpenAI's plugin authentication guide is explicit that the transport-level 401 +
`WWW-Authenticate` challenge our `RemoteAuthProvider` already emits is NOT
enough to make ChatGPT offer account linking:

    "Triggering the tool-level OAuth flow requires both metadata
    (securitySchemes and the resource metadata document) and runtime errors that
    carry _meta["mcp/www_authenticate"]. Without both halves ChatGPT will not
    show the linking UI for that tool."

We had the resource metadata document (`/.well-known/oauth-protected-resource`,
served by `RemoteAuthProvider`) and neither of the other two. This module adds
both:

  1. `securitySchemes` on every tool descriptor in `tools/list`.
  2. `_meta["mcp/www_authenticate"]` on the error result of a tool call that
     failed for an authorization reason.

## Why the `securitySchemes` half is an ASGI rewrite and not a tool argument

`securitySchemes` is an OpenAI extension to the tool descriptor, and this SDK's
wire surface is deliberately closed to anything not in the negotiated protocol
version's schema. Three seams were tried, in increasing desperation, and all
three are dead ends:

  • `@mcp.tool(...)` has no such parameter, and `mcp_types.Tool` silently drops
    unknown kwargs. Not in mcp-types 2.1.1 and not in 2.2.0 either — checked
    against the published wheel, not assumed.
  • A FastMCP `on_list_tools` middleware runs on FastMCP `Tool` objects BEFORE
    `to_mcp_tool()` converts them, and the conversion is what loses the field.
  • Overriding `FastMCP._on_list_tools` (the registered `tools/list` wire
    handler) to return an already-dumped dict gets further and still loses it:
    `mcp.server.runner.Runner._serialize` passes every spec-method result
    through `mcp_types.methods.serialize_server_result`, whose surface models
    carry `extra="ignore"` and therefore strip the field. The surface registry
    (`SERVER_RESULTS`) is a `mappingproxy`, so it cannot be widened either —
    that closure is intentional, and monkeypatching around it would silently
    break on the next SDK bump.

So the injection happens one layer out, on the HTTP response itself, where no
schema sieve applies. That is a real trade — the middleware has to understand
the SSE framing streamable-HTTP uses — but it is honest about being an
out-of-band extension, touches no third-party internals, and cannot be
invalidated by an SDK upgrade. Every message it does not recognise is forwarded
byte-for-byte, so the failure mode is "the field is missing again", never a
corrupted response.

## Why the `_meta` half is a FastMCP middleware

Symmetric problem, easier answer. FastMCP turns a raised `ToolError` into
`CallToolResult(content=[...], is_error=True)` with no `_meta` hook — but a tool
may *return* a `ToolResult`, and `ToolResult.to_mcp_result()` does carry `meta`
through when `meta` or `is_error` is set, and `_meta` survives the surface sieve
(it is part of every version's schema). So the middleware catches the raise and
returns that shape instead. No tool signature changes, and in particular no
change to any tool's return annotation, which would change its published output
schema.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from .client import PicXError
from .settings import get_settings

if TYPE_CHECKING:  # pragma: no cover
    import mcp.types as mcp_types
    from fastmcp.server.middleware import CallNext, MiddlewareContext

logger = logging.getLogger(__name__)

#: Where `RemoteAuthProvider` publishes RFC 9728 protected-resource metadata.
#: Verified live: GET https://mcp.picxstudio.com/.well-known/oauth-protected-resource
#: returns 200 with `resource`, `authorization_servers` and `scopes_supported`.
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"

#: The scopes we declare on every authenticated tool.
#:
#: Deliberately the FULL set on EVERY tool rather than each tool's own required
#: scope, which is the obvious-looking alternative and is broken. OpenAI's guide
#: says to "include the scopes you will request so the consent screen is
#: accurate" — and what ChatGPT then requests is what it read here. picx-studio's
#: `normalise_requested` INTERSECTS an explicit `scope` request and never widens
#: it (see `oauth_as/scopes.py`), so a per-tool declaration of, say, just
#: `images:generate` would mint a grant carrying only that scope. Every other
#: tool would then 403 for a user who was shown no choice and declined nothing —
#: the exact bug that made `DEFAULT_SCOPES` the full set in the first place.
#:
#: Mirrors `SUPPORTED_SCOPES` on picx-studio (itself `SESSION_KEY_SCOPES`, the
#: source of truth) and the `scopes_supported` we publish in
#: `auth.build_auth()`. Kept as a literal because importing `quota.TOOL_SCOPES`
#: would be circular (quota -> context -> auth).
OAUTH_SCOPES: tuple[str, ...] = (
    "images:generate",
    "images:edit",
    "videos:generate",
    "uploads:write",
)


def security_schemes() -> list[dict[str, Any]] | None:
    """The `securitySchemes` array for a tool descriptor, or None to omit it.

    Uniform across all tools because the whole server is token-protected: with
    OAuth configured, `RemoteAuthProvider` 401s every request including the
    read-only tools, so declaring `noauth` on any of them would be a lie that
    makes ChatGPT attempt an anonymous call it cannot complete.

    Returns None when OAuth is not configured, which omits the field entirely —
    per OpenAI, "the tool inherits whatever default the server advertises". That
    is the honest answer for a passthrough deployment: a `pxsk_` API key is
    neither `noauth` nor `oauth2`, and there is no authorization server to link
    to. It also keeps API-key-only deployments byte-identical to today.
    """
    if not get_settings().oauth_configured:
        return None
    return [{"type": "oauth2", "scopes": list(OAUTH_SCOPES)}]


# ─────────────────────────────────────────────────────────────────────────────
# Half 1 — securitySchemes on the tools/list response
# ─────────────────────────────────────────────────────────────────────────────


def inject_security_schemes(message: Any) -> bool:
    """Add `securitySchemes` to each tool in a `tools/list` JSON-RPC result.

    Returns True when the message was modified, so the caller knows whether to
    re-serialize. Mutates in place. Recognises a tools/list result structurally
    (a `result.tools` list of objects with a `name`) rather than by tracking
    request ids, because the rewrite runs on the response stream where the
    matching request is no longer in hand — and because a structural check
    cannot be fooled into mangling some other message shape: anything that does
    not match is left untouched.
    """
    schemes = security_schemes()
    if schemes is None:
        return False
    if not isinstance(message, dict):
        return False
    result = message.get("result")
    if not isinstance(result, dict):
        return False
    tools = result.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    changed = False
    for tool in tools:
        if isinstance(tool, dict) and "name" in tool:
            tool["securitySchemes"] = [dict(scheme) for scheme in schemes]
            changed = True
    return changed


class SecuritySchemesMiddleware:
    """ASGI middleware that adds `securitySchemes` to tool descriptors on egress.

    Wraps the MCP ASGI app (see `server.build_app`). Two response framings are
    handled because streamable HTTP uses either, depending on the client's
    `Accept` header:

      • `text/event-stream` — the common case; FastMCP answers a POST with one
        `event: message` / `data: {…}` frame. Rewritten line-by-line with a
        partial-line buffer, so a payload split across ASGI chunks is still
        handled. No `Content-Length` exists to repair.
      • `application/json` — buffered whole, because changing the body means
        `Content-Length` must be recomputed; sending a stale length is a
        protocol error that manifests as a truncated or hung response.

    Anything else (GET, DELETE, the `.well-known` routes, `/health`, any other
    content type, and every response when OAuth is unconfigured) is passed
    straight through without buffering.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    def __getattr__(self, name: str) -> Any:
        """Delegate unknown attributes to the wrapped app.

        `build_app()` returns this wrapper, so anything that introspects the
        result — `app.routes`, `app.state`, a deployment health check reaching
        for `app.router` — must still see the Starlette app underneath. Without
        this the wrapper would be a silent feature removal for every caller that
        treats the ASGI app as more than a callable.
        """
        return getattr(self.app, name)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Only POST /… carries JSON-RPC responses. Everything else — lifespan,
        # websocket, GET on the well-known routes — must not be touched, and in
        # particular must not be buffered.
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        if security_schemes() is None:
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = {"mode": None, "start": None, "buffer": b"", "chunks": []}

        async def send_wrapper(event: Any) -> None:
            etype = event.get("type")

            if etype == "http.response.start":
                content_type = ""
                for key, value in event.get("headers") or []:
                    if key.lower() == b"content-type":
                        content_type = value.decode("latin-1").lower()
                        break
                if "text/event-stream" in content_type:
                    state["mode"] = "sse"
                    # Headers are unchanged for SSE (no Content-Length), so the
                    # response can start immediately and stay streaming.
                    await send(event)
                elif "application/json" in content_type:
                    state["mode"] = "json"
                    state["start"] = event  # held until the length is known
                else:
                    state["mode"] = "passthrough"
                    await send(event)
                return

            if etype != "http.response.body":
                await send(event)
                return

            mode = state["mode"]
            if mode == "passthrough" or mode is None:
                await send(event)
                return

            body = event.get("body", b"") or b""
            more = event.get("more_body", False)

            if mode == "json":
                state["chunks"].append(body)
                if more:
                    return
                rewritten = self._rewrite_json(b"".join(state["chunks"]))
                start = dict(state["start"] or {})
                start["headers"] = [
                    (key, value)
                    for key, value in (start.get("headers") or [])
                    if key.lower() != b"content-length"
                ] + [(b"content-length", str(len(rewritten)).encode())]
                await send(start)
                await send({"type": "http.response.body", "body": rewritten, "more_body": False})
                return

            # SSE: emit whole lines, hold any partial tail for the next chunk.
            data = state["buffer"] + body
            head, sep, tail = data.rpartition(b"\n")
            if sep:
                state["buffer"] = tail
                out = self._rewrite_sse(head + sep)
            else:
                state["buffer"] = data
                out = b""
            if not more and state["buffer"]:
                out += self._rewrite_sse(state["buffer"])
                state["buffer"] = b""
            if out or not more:
                await send({"type": "http.response.body", "body": out, "more_body": more})

        await self.app(scope, receive, send_wrapper)

    # ── Rewriters ─────────────────────────────────────────────────────────────
    #
    # Both are total: anything that does not parse, or parses to something that
    # is not a tools/list result, is returned unchanged. A rewrite that cannot
    # be made is never a reason to fail a response.

    def _rewrite_json(self, body: bytes) -> bytes:
        try:
            message = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body
        if not inject_security_schemes(message):
            return body
        return json.dumps(message, separators=(",", ":")).encode()

    def _rewrite_sse(self, chunk: bytes) -> bytes:
        if b"data:" not in chunk:
            return chunk
        out: list[bytes] = []
        # keepends=True so `event:` lines, blank frame separators and the exact
        # newline style survive verbatim — only `data:` payloads are touched.
        for line in chunk.splitlines(keepends=True):
            stripped = line.rstrip(b"\r\n")
            if not stripped.startswith(b"data:"):
                out.append(line)
                continue
            ending = line[len(stripped) :]
            payload = stripped[len(b"data:") :]
            leading = payload[: len(payload) - len(payload.lstrip())]
            try:
                message = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                out.append(line)
                continue
            if not inject_security_schemes(message):
                out.append(line)
                continue
            rewritten = json.dumps(message, separators=(",", ":")).encode()
            out.append(b"data:" + leading + rewritten + ending)
        return b"".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Half 2 — _meta["mcp/www_authenticate"] on an authorization failure
# ─────────────────────────────────────────────────────────────────────────────


def _quote(value: str) -> str:
    """Make `value` safe inside a `WWW-Authenticate` quoted-string.

    RFC 7235 quoted-strings cannot contain a raw `"` or a bare CR/LF, and a
    backslash starts an escape. Our challenge descriptions are built from
    model-facing error messages, so they are not adversarial — but they are not
    audited for header syntax either, and one stray quote would produce a
    challenge ChatGPT cannot parse, which fails silently as "no linking UI".
    """
    collapsed = " ".join(value.split())
    return collapsed.replace("\\", " ").replace('"', "'")


def www_authenticate(error: str, description: str) -> str | None:
    """Build the challenge value for `_meta["mcp/www_authenticate"]`.

    Returns None when OAuth is not configured — a challenge naming a resource
    metadata document that would 404 is worse than no challenge at all.

    `error` and `error_description` are both present because OpenAI requires it:
    "make sure the value contains both an error and error_description
    parameter". `resource_metadata` is what points ChatGPT at the authorization
    server (RFC 9728 §5.1).

    Note the value is emitted as a bare header value, NOT wrapped in the single
    quotes OpenAI's doc example shows around the whole string. Those quotes are
    not part of a valid `WWW-Authenticate` value under RFC 7235, and taking them
    literally would make the scheme token `'Bearer` rather than `Bearer`.
    """
    settings = get_settings()
    if not settings.oauth_configured:
        return None
    metadata_url = settings.picx_mcp_base_url.rstrip("/") + RESOURCE_METADATA_PATH
    return (
        f'Bearer resource_metadata="{metadata_url}", '
        f'error="{_quote(error)}", '
        f'error_description="{_quote(description)}"'
    )


class ToolOAuthChallengeMiddleware(Middleware):
    """Turn an authorization failure inside a tool into a linking prompt.

    Only errors that explicitly opted in are converted, via
    `PicXError.oauth_challenge`. Status code alone is NOT a sufficient signal:
    `auth.exchange_token_for_session_key` raises 401 when the connector's own
    internal secret is wrong, and challenging on that would walk the user
    through a login loop for a server misconfiguration they cannot fix. Same for
    the 404 "no PicX account is linked" case — re-authenticating with the same
    identity lands in the same place, so its message (which says to sign up)
    must reach the user unchallenged.

    The realistic trigger is `quota.require_scope`: a grant issued before
    `DEFAULT_SCOPES` became the full scope set carries only `images:generate`,
    so `picx_edit_image` / `picx_generate_video` / `picx_upload_asset` 403 for
    those users with no way to widen the grant. With this challenge attached,
    ChatGPT offers re-linking and the new grant carries all four scopes.
    """

    async def on_call_tool(
        self,
        context: "MiddlewareContext[mcp_types.CallToolRequest]",
        call_next: "CallNext[mcp_types.CallToolRequest, ToolResult]",
    ) -> ToolResult:
        try:
            return await call_next(context)
        except PicXError as exc:
            error = getattr(exc, "oauth_challenge", None)
            if not error:
                raise
            challenge = www_authenticate(error, str(exc))
            if challenge is None:
                raise
            logger.info(
                "Emitting tool-level OAuth challenge (error=%s) for tool %s",
                error,
                getattr(getattr(context, "message", None), "name", "<unknown>"),
            )
            return ToolResult(
                content=[TextContent(type="text", text=str(exc))],
                meta={"mcp/www_authenticate": [challenge]},
                is_error=True,
            )
