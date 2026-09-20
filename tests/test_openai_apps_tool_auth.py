"""OpenAI Apps tool-level OAuth signalling — both halves, verified on the wire.

OpenAI's plugin authentication guide states that ChatGPT will not offer account
linking for a tool unless the server provides BOTH:

    1. `securitySchemes` on the tool descriptor (plus the resource metadata
       document, which `test_oauth_401_challenge.py` already covers), and
    2. a tool error result carrying `_meta["mcp/www_authenticate"]`.

Both are protocol extensions that the installed SDK's models do not know about
(`mcp_types.Tool` has no `securitySchemes` field and drops unknown kwargs;
FastMCP's raise->error-result path has no `_meta` hook), so they are implemented
in `picx_mcp.openai_apps` through the two seams that do survive serialization.

That is exactly why these tests drive the REAL ASGI app over HTTP and read the
REAL JSON-RPC bytes rather than asserting on Python objects: a unit test on
`_on_list_tools`'s return value would still pass if the field were silently
dropped by pydantic on the way out, which is the failure mode being guarded
against. Only the JWKS key fetch is stubbed; token minting, signature
verification, `iss`/`aud` checks and the whole request path are real.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from fastmcp.server.auth.providers.jwt import JWTVerifier

from picx_mcp import server
from picx_mcp.openai_apps import (
    OAUTH_SCOPES,
    RESOURCE_METADATA_PATH,
    SecuritySchemesMiddleware,
    security_schemes,
    www_authenticate,
)
from picx_mcp.settings import Settings

ISSUER = "https://api.picxstudio.com"
RESOURCE = "https://mcp.picxstudio.com"

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _settings(**overrides: object) -> Settings:
    base = dict(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url=RESOURCE,
        redis_url="redis://localhost:6379",
        request_state_key="x" * 32,
        picx_internal_secret="internal-secret",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _patch_settings(settings: Settings):
    """Patch every module path build_app() reads settings through.

    Mirrors test_oauth_401_challenge._app_under — miss one and the app
    half-configures. `openai_apps` is a fourth reader, so it is patched too:
    without it `security_schemes()` would consult the process environment
    instead of the test's settings.
    """
    targets = (
        "picx_mcp.auth.get_settings",
        "picx_mcp.server.get_settings",
        "picx_mcp.settings.get_settings",
        "picx_mcp.openai_apps.get_settings",
    )
    patches = [patch(t, return_value=settings) for t in targets]
    for p in patches:
        p.start()
    return patches


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


def _mint(priv_pem: str, *, scope: str = "") -> str:
    """An access token shaped exactly like one picx-studio mints.

    `aud` is the SLASHED spelling on purpose: that is what ChatGPT sends as the
    `resource` parameter (it reads the published metadata, which normalises the
    trailing slash) and therefore what the authorization server echoes into
    `aud`. Commit 17fdeb7 made the verifier accept both spellings; minting the
    slashed one here keeps these tests on the path a real ChatGPT grant takes.
    """
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": RESOURCE + "/",
            "sub": "11111111-2222-3333-4444-555555555555",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scope": scope,
        },
        priv_pem,
        algorithm="RS256",
    )


def _rpc(client: TestClient, body: dict, token: str | None = None) -> dict:
    """POST a JSON-RPC request and return the `result` object.

    Streamable HTTP may answer with either `application/json` or an SSE stream
    depending on what the server picks, so both framings are handled — the tests
    are about the payload, not the framing.
    """
    headers = dict(_MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.post("/", json=body, headers=headers)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:400]}"
    text = resp.text
    if "text/event-stream" in resp.headers.get("content-type", ""):
        payloads = [
            line[len("data:") :].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        assert payloads, f"SSE response carried no data frame: {text[:400]}"
        message = json.loads(payloads[-1])
    else:
        message = resp.json()
    assert "error" not in message, f"JSON-RPC error: {message['error']}"
    return message["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Half 1 — securitySchemes on every tool descriptor
# ─────────────────────────────────────────────────────────────────────────────


class TestSecuritySchemesOnTheWire:
    def test_every_tool_declares_oauth2_with_the_full_scope_set(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """tools/list must carry securitySchemes on each of the 18 tools.

        The full scope set on EVERY tool is deliberate, not sloppy: picx-studio's
        `normalise_requested` intersects an explicit `scope` request and never
        widens it, so a per-tool declaration of one scope would mint a grant
        carrying only that scope and every other tool would 403.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        try:
            with patch.object(
                JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        token=_mint(priv),
                    )
        finally:
            for p in patches:
                p.stop()

        tools = result["tools"]
        assert len(tools) == 18, f"expected 18 tools, got {len(tools)}"

        missing = [t["name"] for t in tools if "securitySchemes" not in t]
        assert not missing, (
            "These tools reached the wire without securitySchemes, so ChatGPT "
            f"will not offer linking for them: {missing}"
        )

        expected = [{"type": "oauth2", "scopes": list(OAUTH_SCOPES)}]
        wrong = {
            t["name"]: t["securitySchemes"]
            for t in tools
            if t["securitySchemes"] != expected
        }
        assert not wrong, f"unexpected securitySchemes: {wrong}"

    def test_descriptors_are_otherwise_unchanged(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """Injecting the field must not disturb the rest of the descriptor.

        The override re-dumps the SDK model itself rather than rebuilding it, so
        name/title/description/inputSchema/annotations must survive verbatim. A
        submission review may already have scanned this metadata; drift in any
        other field would be a worse problem than the missing field this change
        fixes.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        try:
            with patch.object(
                JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                        token=_mint(priv),
                    )
        finally:
            for p in patches:
                p.stop()

        by_name = {t["name"]: t for t in result["tools"]}
        generate = by_name["picx_generate_image"]
        assert generate["title"] == "Generate Image"
        assert "inputSchema" in generate and generate["inputSchema"]["type"] == "object"
        assert generate["annotations"]["readOnlyHint"] is False
        assert "prompt" in generate["inputSchema"]["properties"]

        # And a read-only tool keeps its own truthful annotation.
        assert by_name["picx_list_models"]["annotations"]["readOnlyHint"] is True

    def test_omitted_in_passthrough_mode(self) -> None:
        """With no issuer the field must be absent, not `noauth`.

        An API-key deployment is neither `noauth` (a pxsk_ IS required) nor
        `oauth2` (there is no authorization server to link to), and OpenAI's
        documented meaning of an omitted array — inherit the server default — is
        the only honest answer. This also keeps dev deployments byte-identical to
        the metadata that shipped before this change.
        """
        patches = _patch_settings(_settings(picx_auth_issuer=None))
        try:
            app = server.build_app()
            with TestClient(app) as client:
                result = _rpc(
                    client,
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
        finally:
            for p in patches:
                p.stop()

        offenders = [t["name"] for t in result["tools"] if "securitySchemes" in t]
        assert not offenders, (
            "Passthrough mode must advertise no securitySchemes; these declared "
            f"one: {offenders}"
        )
        assert len(result["tools"]) == 18


# ─────────────────────────────────────────────────────────────────────────────
# Half 2 — _meta["mcp/www_authenticate"] on an authorization failure
# ─────────────────────────────────────────────────────────────────────────────


class TestToolErrorCarriesChallenge:
    def test_insufficient_scope_error_carries_www_authenticate(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """A scope-short grant must come back as an error result WITH the challenge.

        This is the live failure this half fixes: a grant minted before
        DEFAULT_SCOPES became the full set carries only `images:generate`, so the
        other three spending tools 403 with no way for the user to widen the
        grant. The token here carries NO scopes, which is the same shape from
        `require_scope`'s point of view.

        Note the transport-level 401 cannot cover this case — the token is
        perfectly valid, so auth middleware lets it through and the refusal
        happens inside the tool, where only `_meta` can carry a challenge.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        try:
            with patch.object(
                JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "picx_generate_image",
                                "arguments": {"prompt": "a test prompt"},
                            },
                        },
                        token=_mint(priv, scope=""),
                    )
        finally:
            for p in patches:
                p.stop()

        assert result.get("isError") is True, f"expected an error result, got {result}"

        meta = result.get("_meta") or {}
        challenges = meta.get("mcp/www_authenticate")
        assert challenges, (
            "The error result carries no _meta['mcp/www_authenticate'], so "
            f"ChatGPT will show no linking UI. Result: {result}"
        )
        challenge = challenges[0]
        assert challenge.startswith("Bearer "), challenge
        assert f'resource_metadata="{RESOURCE}{RESOURCE_METADATA_PATH}"' in challenge
        assert 'error="insufficient_scope"' in challenge
        assert "error_description=" in challenge

        text = " ".join(
            block.get("text", "") for block in result.get("content", [])
        )
        assert "images:generate" in text, (
            "The human-readable message must still name the missing scope; got "
            f"{text!r}"
        )

    def test_non_auth_failure_is_not_challenged(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """A validation error must NOT carry a challenge.

        Challenging on every error would send users into a login loop for
        problems logging in cannot fix. Only errors that explicitly opt in via
        `PicXError.oauth_challenge` are converted, so an empty prompt — which
        fails before any auth check — must come back as a plain error result.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        try:
            with patch.object(
                JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "picx_generate_image",
                                "arguments": {"prompt": "   "},
                            },
                        },
                        token=_mint(priv),
                    )
        finally:
            for p in patches:
                p.stop()

        assert result.get("isError") is True
        meta = result.get("_meta") or {}
        assert "mcp/www_authenticate" not in meta, (
            "A validation failure must not trigger the linking UI — "
            f"got {meta}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Challenge string construction
# ─────────────────────────────────────────────────────────────────────────────


class TestChallengeConstruction:
    def test_none_when_oauth_unconfigured(self) -> None:
        """No issuer means no challenge — pointing at a 404 document is worse."""
        patches = _patch_settings(_settings(picx_auth_issuer=None))
        try:
            assert www_authenticate("insufficient_scope", "nope") is None
            assert security_schemes() is None
        finally:
            for p in patches:
                p.stop()

    def test_quotes_and_newlines_are_neutralised(self) -> None:
        """A stray `"` in a description would make the challenge unparseable.

        Descriptions are built from model-facing error messages that are not
        audited for header syntax, and a malformed challenge fails silently as
        "no linking UI" rather than as an error anyone would notice.
        """
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        try:
            challenge = www_authenticate(
                "insufficient_scope", 'he said "no"\nand\\then left'
            )
        finally:
            for p in patches:
                p.stop()

        assert challenge is not None
        # Exactly three quoted values: resource_metadata, error, error_description.
        assert challenge.count('"') == 6, challenge
        assert "\n" not in challenge
        assert "\\" not in challenge
        assert "he said 'no' and then left" in challenge

    def test_resource_metadata_url_has_no_double_slash(self) -> None:
        """The base URL may or may not carry a trailing slash; the URL must not."""
        patches = _patch_settings(
            _settings(picx_auth_issuer=ISSUER, picx_mcp_base_url=RESOURCE + "/")
        )
        try:
            challenge = www_authenticate("invalid_token", "x")
        finally:
            for p in patches:
                p.stop()

        assert challenge is not None
        assert f"{RESOURCE}{RESOURCE_METADATA_PATH}" in challenge
        assert "//.well-known" not in challenge



# ─────────────────────────────────────────────────────────────────────────────
# The ASGI rewrite itself — framing, chunk splits, and what it must NOT touch
# ─────────────────────────────────────────────────────────────────────────────


async def _drive(
    middleware: SecuritySchemesMiddleware,
    events: list[dict],
    *,
    method: str = "POST",
) -> list[dict]:
    """Run `middleware` over a canned downstream response and collect egress."""
    sent: list[dict] = []

    async def inner_app(scope, receive, send):  # noqa: ANN001
        for event in events:
            await send(event)

    middleware.app = inner_app

    async def send(event):  # noqa: ANN001
        sent.append(event)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware({"type": "http", "method": method, "path": "/"}, receive, send)
    return sent


def _sse(*frames: dict) -> list[dict]:
    body = b"".join(
        b"event: message\ndata: " + json.dumps(f).encode() + b"\n\n" for f in frames
    )
    return [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {"type": "http.response.body", "body": body, "more_body": False},
    ]


def _collect(sent: list[dict]) -> bytes:
    return b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")


_TOOLS_LIST = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "tools": [{"name": "picx_get_account", "inputSchema": {"type": "object"}}],
        "resultType": "complete",
    },
}


class TestSecuritySchemesMiddlewareMechanics:
    @pytest.fixture(autouse=True)
    def _oauth_on(self):
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))
        yield
        for p in patches:
            p.stop()

    async def test_sse_payload_split_across_chunks_is_still_rewritten(self) -> None:
        """A `data:` line arriving in two ASGI chunks must not be missed.

        The middleware holds a partial tail rather than rewriting per-chunk,
        because a naive per-chunk rewrite would silently skip any frame the
        transport happened to split — a load-dependent bug that would look like
        "securitySchemes works locally, disappears in production".
        """
        whole = _sse(_TOOLS_LIST)[1]["body"]
        split = len(whole) // 2
        events = [
            _sse(_TOOLS_LIST)[0],
            {"type": "http.response.body", "body": whole[:split], "more_body": True},
            {"type": "http.response.body", "body": whole[split:], "more_body": False},
        ]
        sent = await _drive(SecuritySchemesMiddleware(None), events)
        body = _collect(sent)
        assert b"securitySchemes" in body, body
        # Framing preserved verbatim.
        assert body.startswith(b"event: message\ndata: ")
        assert body.endswith(b"\n\n")
        payload = json.loads(body.split(b"data: ", 1)[1].rstrip(b"\n"))
        assert payload["result"]["tools"][0]["securitySchemes"] == [
            {"type": "oauth2", "scopes": list(OAUTH_SCOPES)}
        ]

    async def test_other_messages_pass_through_byte_identical(self) -> None:
        """Only a tools/list result may be altered.

        Recognition is structural (`result.tools` holding named objects), so the
        guarantee under test is that everything else — a tool call result, an
        error, a ping — reaches the client exactly as the SDK wrote it.
        """
        others = [
            {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "resultType": "complete"}},
            {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "nope"}},
            {"jsonrpc": "2.0", "id": 4, "result": {"resultType": "complete"}},
        ]
        events = _sse(*others)
        expected = events[1]["body"]
        sent = await _drive(SecuritySchemesMiddleware(None), events)
        assert _collect(sent) == expected

    async def test_json_framing_recomputes_content_length(self) -> None:
        """A rewritten `application/json` body must carry the NEW length.

        A stale Content-Length is not a cosmetic bug: the client reads the
        advertised number of bytes and either truncates the JSON or blocks
        waiting for bytes that never arrive.
        """
        body = json.dumps(_TOOLS_LIST).encode()
        events = [
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            },
            {"type": "http.response.body", "body": body, "more_body": False},
        ]
        sent = await _drive(SecuritySchemesMiddleware(None), events)
        start = next(e for e in sent if e["type"] == "http.response.start")
        out = _collect(sent)
        lengths = [v for k, v in start["headers"] if k.lower() == b"content-length"]
        assert lengths == [str(len(out)).encode()], (
            f"Content-Length {lengths} does not match the {len(out)}-byte body"
        )
        assert b"securitySchemes" in out

    async def test_non_post_is_not_buffered(self) -> None:
        """GET responses stream through untouched.

        `/health`, the `.well-known` documents and the SSE GET stream all arrive
        as non-POST and must not be intercepted — buffering a long-lived stream
        would hang it.
        """
        events = [
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            },
            {"type": "http.response.body", "body": b"data: {\"tools\": []}\n\n"},
        ]
        sent = await _drive(SecuritySchemesMiddleware(None), events, method="GET")
        assert sent == events

    async def test_malformed_data_frame_is_forwarded_unchanged(self) -> None:
        """An unparseable frame is passed on, never dropped or corrupted.

        The rewrite is best-effort by design: the worst outcome it may produce is
        a missing `securitySchemes`, never a broken response.
        """
        events = [
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            },
            {
                "type": "http.response.body",
                "body": b"event: message\ndata: {not json at all\n\n",
                "more_body": False,
            },
        ]
        sent = await _drive(SecuritySchemesMiddleware(None), events)
        assert _collect(sent) == events[1]["body"]


class TestPassthroughModeSkipsTheRewrite:
    async def test_no_interception_when_oauth_unconfigured(self) -> None:
        """With no issuer the middleware must be a pure no-op.

        Not merely "omits the field": it must not buffer or re-frame anything, so
        an API-key deployment behaves exactly as it did before this change.
        """
        patches = _patch_settings(_settings(picx_auth_issuer=None))
        try:
            events = _sse(_TOOLS_LIST)
            sent = await _drive(SecuritySchemesMiddleware(None), events)
        finally:
            for p in patches:
                p.stop()
        assert sent == events
