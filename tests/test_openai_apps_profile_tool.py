"""OpenAI Apps `picx_get_profile` tool — verified on the wire.

OpenAI's Apps authentication guide specifies an optional profile tool so
ChatGPT can tell multiple linked accounts apart. The identity it returns has a
fixed contract:

    - the descriptor's `_meta` carries `"openai/profile": true`,
    - annotations are readOnlyHint/destructiveHint/openWorldHint = true/false/false,
    - the output is ONE object with fields at TOP level (`id` required, optional
      `name`/`email`/`nickname`, additionalProperties false),
    - `id` is opaque and stable and is NOT the email — we use the verified OAuth
      `sub`, which IS the PicX `User.id` UUID,
    - the object is returned in BOTH `structuredContent` and a JSON text block.

Every one of those facts is a wire fact. The entire risk this module guards
against is that a field is silently STRIPPED on egress: the SDK runner sieves
every spec-method result through `mcp_types.methods.serialize_server_result`,
whose surface models carry `extra="ignore"`. A unit test on the Python return
value would still pass while the wire dropped the field, so — exactly like
`test_openai_apps_tool_auth.py` — these tests drive the REAL ASGI app over HTTP
and read the REAL JSON-RPC bytes. Only the JWKS key fetch is stubbed; token
minting, signature verification, `iss`/`aud` checks and the whole request path
are real.
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
from picx_mcp.settings import Settings

ISSUER = "https://api.picxstudio.com"
RESOURCE = "https://mcp.picxstudio.com"
SUBJECT = "11111111-2222-3333-4444-555555555555"

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — copied from test_openai_apps_tool_auth.py so the two suites drive
# the app the same way.
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
    """Patch every module path build_app() reads settings through (all four)."""
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


def _mint(priv_pem: str, *, scope: str = "", sub: str = SUBJECT) -> str:
    """An access token shaped exactly like one picx-studio mints.

    `aud` is the SLASHED spelling on purpose — that is what ChatGPT sends as the
    `resource` parameter and therefore what the AS echoes into `aud`. `sub` is a
    UUID because it IS the PicX `User.id`, which is what the profile `id` must be.
    """
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": RESOURCE + "/",
            "sub": sub,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "scope": scope,
        },
        priv_pem,
        algorithm="RS256",
    )


def _rpc(client: TestClient, body: dict, token: str | None = None) -> dict:
    """POST a JSON-RPC request and return the `result` object, parsed from bytes."""
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
# The profile tool descriptor in tools/list
# ─────────────────────────────────────────────────────────────────────────────


class TestProfileToolDescriptorOnTheWire:
    def test_descriptor_carries_meta_annotations_and_output_schema(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """The whole descriptor contract, asserted from the parsed wire bytes.

        `_meta["openai/profile"]` is the load-bearing signal: if the surface
        sieve stripped it (which is the failure mode this module exists to
        avoid) ChatGPT would never treat this as a profile tool. FastMCP's own
        `_meta.fastmcp` must ALSO survive — the guide's `meta={...}` MERGES with
        it rather than replacing it, and that merge is what keeps tags on wire.
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
        assert "picx_get_profile" in by_name, (
            "picx_get_profile did not reach tools/list on the wire: "
            f"{sorted(by_name)}"
        )
        tool = by_name["picx_get_profile"]

        # _meta: the OpenAI signal survived AND FastMCP's own block survived.
        meta = tool.get("_meta") or {}
        assert meta.get("openai/profile") is True, (
            "openai/profile was stripped on egress or not merged into _meta; "
            f"got {meta}"
        )
        assert "fastmcp" in meta, (
            "FastMCP's own _meta.fastmcp block was lost — the meta= kwarg "
            f"replaced it instead of merging. Got {meta}"
        )

        # annotations, exactly as OpenAI's guide specifies.
        ann = tool.get("annotations") or {}
        assert ann.get("readOnlyHint") is True, ann
        assert ann.get("destructiveHint") is False, ann
        assert ann.get("openWorldHint") is False, ann

        # output schema: top-level fields, id required, additionalProperties off.
        schema = tool.get("outputSchema") or {}
        assert schema.get("type") == "object", schema
        assert schema.get("required") == ["id"], schema
        assert schema.get("additionalProperties") is False, schema
        props = schema.get("properties") or {}
        assert set(props) >= {"id", "name", "email", "nickname"}, props

    def test_profile_tool_also_carries_securityschemes(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """The new tool must not be a hole in the linking metadata.

        SecuritySchemesMiddleware injects `securitySchemes` onto every tool in
        tools/list when OAuth is configured; a tool it missed would 403 an
        anonymous call ChatGPT can't complete. Nineteen tools now, up from 18.
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
        assert len(tools) == 19, f"expected 19 tools, got {len(tools)}"
        profile = next(t for t in tools if t["name"] == "picx_get_profile")
        assert "securitySchemes" in profile, profile


# ─────────────────────────────────────────────────────────────────────────────
# tools/call — the profile object in BOTH structuredContent and text
# ─────────────────────────────────────────────────────────────────────────────


class TestProfileToolCallOnTheWire:
    def test_id_equals_token_sub_in_structured_and_text(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """A call returns `id` == the verified `sub`, in both wire locations.

        `/account/me` is stubbed to fail so the enrichment path degrades — this
        pins the guarantee that the required `id` comes from the token subject
        with NO network call, and that a failed enrichment still yields a valid
        id-only profile rather than an error.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))

        # Force the enrichment path to fail (as a real deployment would when the
        # token->session-key exchange or /account/me is unreachable): id must
        # still come from `sub`, and the tool must NOT error.
        async def _boom():
            raise RuntimeError("account endpoint unavailable")

        try:
            with (
                patch.object(
                    JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
                ),
                patch("picx_mcp.tools.account.get_client", _boom),
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "picx_get_profile", "arguments": {}},
                        },
                        token=_mint(priv),
                    )
        finally:
            for p in patches:
                p.stop()

        assert result.get("isError") is not True, f"tool errored: {result}"

        # structuredContent carries the profile with id == sub.
        structured = result.get("structuredContent")
        assert structured is not None, f"no structuredContent on the wire: {result}"
        assert structured.get("id") == SUBJECT, structured
        # Degraded: no enrichment fields leaked in.
        assert "email" not in structured, structured

        # The same object appears, serialized as JSON, in a text content block.
        texts = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
        assert texts, f"no text content block: {result}"
        parsed = [json.loads(t) for t in texts if t.strip().startswith("{")]
        assert any(p.get("id") == SUBJECT for p in parsed), (
            "no text content block carried the profile JSON with id == sub; "
            f"texts={texts}"
        )

    def test_enrichment_populates_name_and_email(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """When `/account/me` responds, name/email enrich the profile.

        `id` still comes from the opaque token `sub`, NOT from the account
        email — OpenAI is explicit that email is display-only, not identity.
        """
        priv, pub = rsa_keypair
        patches = _patch_settings(_settings(picx_auth_issuer=ISSUER))

        # Stub get_client() itself: the real one runs the OAuth token->session-key
        # exchange (a live call to picx-studio) before returning, which isn't
        # reachable in a unit context. We only care that a working client yields
        # /account/me enrichment, so hand the tool a fake client directly.
        class _FakeClient:
            async def get(self, path, **kwargs):  # noqa: ANN001, ANN002
                assert path == "/account/me", path
                return {
                    "id": "account-row-id-not-the-subject",
                    "name": "Ada Lovelace",
                    "email": "ada@example.com",
                }

        async def _get_client():
            return _FakeClient()

        try:
            with (
                patch.object(
                    JWTVerifier, "_get_verification_key", AsyncMock(return_value=pub)
                ),
                patch("picx_mcp.tools.account.get_client", _get_client),
            ):
                app = server.build_app()
                with TestClient(app) as client:
                    result = _rpc(
                        client,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {"name": "picx_get_profile", "arguments": {}},
                        },
                        token=_mint(priv),
                    )
        finally:
            for p in patches:
                p.stop()

        structured = result.get("structuredContent") or {}
        # id is the OPAQUE subject, not the account row id and not the email.
        assert structured.get("id") == SUBJECT, structured
        assert structured.get("id") != "ada@example.com"
        assert structured.get("name") == "Ada Lovelace", structured
        assert structured.get("email") == "ada@example.com", structured
