"""Behavioural verification of spec Task 4 — the 401 challenge and token verification.

`test_auth.py` already proves the *construction* of the resource-server provider
(`build_auth()` returns a `RemoteAuthProvider` wrapping a `JWTVerifier`) and the
*presence* of its `.well-known` route. What it does NOT prove is the thing the
whole ChatGPT/Claude plugin flow hinges on: that wiring that provider into
`FastMCP(auth=…)` actually makes the running ASGI app

  • answer an unauthenticated tool call with HTTP 401 + a `WWW-Authenticate`
    challenge naming the protected-resource metadata URL — NOT a 200 with the
    error buried in the JSON-RPC body. A 200 never triggers a host's OAuth flow;
    that 200-with-embedded-error shape is precisely the pre-Task-4 bug (an MCP
    host reads it as "the call merely failed" and shows "go get an API key",
    which is the ~90% plugin dropout the spec calls out).
  • serve `/.well-known/oauth-protected-resource` (200, `authorization_servers`
    naming the issuer) when OAuth is configured, and NOT serve it (404) when it
    is unconfigured — the shape every deployment runs today.
  • reject a token whose `iss` or `aud` is wrong, at the `JWTVerifier` level.

These are driven through the real ASGI app (Starlette `TestClient`) and the real
installed `fastmcp==4.0.0b3` verifier — no mock of the auth machinery itself, so
a future signature/behaviour drift fails HERE instead of on first production boot.

Verification method for group C: rather than stand up a live JWKS endpoint, the
`JWTVerifier._get_verification_key` hook is stubbed to return a locally-generated
RSA public key, and tokens are minted with PyJWT RS256 against the matching
private key. This exercises the verifier's real claim checks (signature, `iss`,
`aud`, `exp`) — only the key-transport (JWKS fetch) is stubbed.
"""

from __future__ import annotations

import asyncio
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
PRM_PATH = "/.well-known/oauth-protected-resource"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _settings(**overrides: object) -> Settings:
    base = dict(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url=RESOURCE,
        redis_url="redis://localhost:6379",
        request_state_key="x" * 32,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _app_under(settings: Settings):
    """Build the real ASGI app with get_settings patched everywhere it is read.

    build_server()/build_app() read settings through three module paths (auth,
    server, settings). All three are patched, matching test_auth.py's own
    _well_known_routes helper — miss one and the app half-configures.
    """
    targets = (
        "picx_mcp.auth.get_settings",
        "picx_mcp.server.get_settings",
        "picx_mcp.settings.get_settings",
    )
    patches = [patch(t, return_value=settings) for t in targets]
    for p in patches:
        p.start()
    try:
        return server.build_app(), patches
    except Exception:
        for p in patches:
            p.stop()
        raise


def _tool_call_body(name: str = "picx_get_account") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


# ─────────────────────────────────────────────────────────────────────────────
# Group A — the 401 + WWW-Authenticate challenge (spec 4.1, 4.2)
# ─────────────────────────────────────────────────────────────────────────────


class TestUnauthenticatedChallenge:
    """An unauthenticated tool call must be challenged at the HTTP layer."""

    def test_no_auth_header_returns_http_401_not_200(self) -> None:
        """A no-credential tool call is HTTP 401 — not a 200 with an embedded error.

        This is the single property the plugin flow depends on: a host only
        starts OAuth on a 401. If this asserts 200, the connector is back in the
        pre-Task-4 broken state where the error is buried in the JSON-RPC body
        and the host shows "go get an API key" instead of an auth prompt.
        """
        app, patches = _app_under(_settings(picx_auth_issuer=ISSUER))
        try:
            client = TestClient(app)
            resp = client.post("/", json=_tool_call_body(), headers=_MCP_HEADERS)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 401, (
            f"Expected HTTP 401 on an unauthenticated tool call, got "
            f"{resp.status_code}. A 200 (error inside the JSON-RPC body) never "
            f"triggers a host's OAuth flow — that is the pre-Task-4 bug."
        )

    def test_www_authenticate_header_names_protected_resource_metadata(self) -> None:
        """The 401 must carry a WWW-Authenticate challenge pointing at PRM metadata.

        RFC 9728 §5.1: the challenge advertises the protected-resource metadata
        URL so a host can discover where to authenticate cold. Without this
        header the 401 is just a rejection with no path forward.
        """
        app, patches = _app_under(_settings(picx_auth_issuer=ISSUER))
        try:
            client = TestClient(app)
            resp = client.post("/", json=_tool_call_body(), headers=_MCP_HEADERS)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 401
        challenge = resp.headers.get("www-authenticate")
        assert challenge is not None, "401 is missing the WWW-Authenticate header"
        assert challenge.lower().startswith("bearer"), (
            f"WWW-Authenticate must be a Bearer challenge, got {challenge!r}"
        )
        assert PRM_PATH in challenge, (
            f"WWW-Authenticate must name the protected-resource metadata URL "
            f"({PRM_PATH}); got {challenge!r}"
        )
        assert "resource_metadata" in challenge, (
            f"WWW-Authenticate must carry resource_metadata=…; got {challenge!r}"
        )

    def test_passthrough_mode_does_not_challenge(self) -> None:
        """With OAuth unconfigured there is no auth middleware, so no 401 challenge.

        The pxsk_ passthrough deployment authenticates *inside* the tool
        (context.resolve_api_key), not at the HTTP layer, so an unauthenticated
        call is NOT met with a WWW-Authenticate challenge. Asserting the absence
        of the challenge here guards against accidentally arming OAuth middleware
        in a deployment that has no issuer to honour it.

        Unlike the OAuth-mode tests, this request has no auth middleware to
        short-circuit it, so it flows into the StreamableHTTP session manager —
        which requires the ASGI lifespan to be running. `TestClient` as a context
        manager runs that lifespan; a bare `TestClient(app)` does not, and the
        call raises "Task group is not initialized". The tool itself then rejects
        the missing pxsk_ inside the JSON-RPC body (a 200 in passthrough mode);
        we assert only the ABSENCE of the OAuth challenge header, which is the
        property under test.
        """
        app, patches = _app_under(_settings(picx_auth_issuer=None))
        try:
            with TestClient(app) as client:
                resp = client.post("/", json=_tool_call_body(), headers=_MCP_HEADERS)
        finally:
            for p in patches:
                p.stop()

        assert resp.headers.get("www-authenticate") is None, (
            "Passthrough mode must not emit a WWW-Authenticate challenge — no "
            "OAuth middleware should be installed when the issuer is unset."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group B — protected-resource metadata served / absent (spec 3.1, 4.2)
# ─────────────────────────────────────────────────────────────────────────────


class TestProtectedResourceMetadata:
    """The RFC 9728 document must be served (200) when OAuth is on, 404 when off."""

    def test_served_with_issuer_in_authorization_servers(self) -> None:
        app, patches = _app_under(_settings(picx_auth_issuer=ISSUER))
        try:
            client = TestClient(app)
            resp = client.get(PRM_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200, (
            f"{PRM_PATH} must return 200 when OAuth is configured, got "
            f"{resp.status_code}"
        )
        body = resp.json()
        servers = [str(s).rstrip("/") for s in body.get("authorization_servers", [])]
        assert ISSUER in servers, (
            f"protected-resource metadata must name the issuer {ISSUER!r} in "
            f"authorization_servers; got {servers!r}"
        )
        # The advertised resource must be this connector, so a host validates the
        # round-trip against the right audience.
        assert str(body.get("resource", "")).rstrip("/") == RESOURCE

    def test_absent_when_unconfigured(self) -> None:
        """With no issuer the resource-server surface must not exist (404 is fine).

        This matches current production: PICX_AUTH_ISSUER is unset, so
        https://mcp.picxstudio.com/.well-known/oauth-protected-resource 404s.
        """
        app, patches = _app_under(_settings(picx_auth_issuer=None))
        try:
            client = TestClient(app)
            resp = client.get(PRM_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 404, (
            f"{PRM_PATH} must be absent (404) in passthrough mode, got "
            f"{resp.status_code} — an API-key-only deployment must advertise no "
            f"OAuth surface it cannot honour."
        )

    def test_authorization_server_metadata_not_served(self) -> None:
        """A pure resource server must NOT serve authorization-server metadata.

        That document is picx-studio's responsibility under the revised topology;
        the connector serving it would misrepresent itself as an issuer.
        """
        app, patches = _app_under(_settings(picx_auth_issuer=ISSUER))
        try:
            client = TestClient(app)
            resp = client.get("/.well-known/oauth-authorization-server")
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 404, (
            "The connector must not serve /.well-known/oauth-authorization-server "
            "— it is a resource server, not an authorization server."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Group C — JWTVerifier claim checks: wrong iss / wrong aud rejected (spec 4.1)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """A throwaway RSA keypair (PEM strings): (private, public)."""
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


def _mint(priv_pem: str, *, iss: str = ISSUER, aud: str = RESOURCE, exp_delta: int = 3600) -> str:
    """Mint an RS256 access token with the given claims."""
    return jwt.encode(
        {
            "iss": iss,
            "aud": aud,
            "sub": "oauth-user-1",
            "exp": int(time.time()) + exp_delta,
            "iat": int(time.time()),
            "scope": "",
        },
        priv_pem,
        algorithm="RS256",
    )


def _verify(verifier: JWTVerifier, token: str, pub_pem: str):
    """Run verify_token with the JWKS fetch stubbed to return the public key."""

    async def _run():
        with patch.object(
            verifier, "_get_verification_key", AsyncMock(return_value=pub_pem)
        ):
            return await verifier.verify_token(token)

    return asyncio.run(_run())


class TestJWTVerifierClaimChecks:
    """The verifier's iss/aud checks are the resource server's trust decision."""

    def _verifier(self) -> JWTVerifier:
        # Built exactly as build_auth() builds it (see auth.py).
        return JWTVerifier(
            jwks_uri=f"{ISSUER}/.well-known/jwks.json",
            issuer=ISSUER,
            audience=RESOURCE,
        )

    def test_valid_token_accepted(self, rsa_keypair: tuple[str, str]) -> None:
        priv, pub = rsa_keypair
        result = _verify(self._verifier(), _mint(priv), pub)
        assert result is not None, "A correctly-signed, correctly-claimed token was rejected"

    def test_wrong_issuer_rejected(self, rsa_keypair: tuple[str, str]) -> None:
        priv, pub = rsa_keypair
        token = _mint(priv, iss="https://evil.example.com")
        assert _verify(self._verifier(), token, pub) is None, (
            "A token whose iss is not the configured issuer must be rejected — "
            "otherwise any issuer's tokens would be accepted."
        )

    def test_wrong_audience_rejected(self, rsa_keypair: tuple[str, str]) -> None:
        priv, pub = rsa_keypair
        token = _mint(priv, aud="https://not-picx.example.com")
        assert _verify(self._verifier(), token, pub) is None, (
            "A token minted for a different resource (aud) must be rejected — "
            "RFC 8707 audience binding is what stops token replay across resources."
        )

    def test_expired_token_rejected(self, rsa_keypair: tuple[str, str]) -> None:
        priv, pub = rsa_keypair
        token = _mint(priv, exp_delta=-60)
        assert _verify(self._verifier(), token, pub) is None, (
            "An expired token must be rejected."
        )
