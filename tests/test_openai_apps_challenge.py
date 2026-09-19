"""Behavioural verification of the OpenAI Apps domain-verification challenge route.

OpenAI's submission portal verifies domain ownership by asking the MCP server to
serve a token it generates at GET /.well-known/openai-apps-challenge. OpenAI's
requirement (verbatim): "place the exact verification token at the generated
well-known URL... The challenge endpoint must return only that plugin's
verification token. Do not return JSON, a list of tokens, or multiple tokens
from the same URL."

So the properties under test are:

  • UNSET (default / current production): the route 404s. The live URL 404s today
    and must keep 404ing until a human sets the token — this route deploys inert,
    exactly like picx_auth_issuer gates OAuth. An empty string must ALSO 404
    (never serve an empty/placeholder token — a wrong-but-200 token would be
    worse than a clean 404, as the portal could read it as a valid attempt).
  • SET: the route returns the token as the RAW body string (no JSON wrapper, no
    list, no HTML) with Content-Type text/plain and HTTP 200.

Driven through the real ASGI app (Starlette TestClient) with get_settings patched
in all three module paths, matching the fixture style of test_oauth_401_challenge.py.
Because the route is a @mcp.custom_route (not behind auth middleware), the request
flows into the StreamableHTTP session manager, which requires the ASGI lifespan —
so TestClient is used as a context manager (a bare TestClient(app) raises
"Task group is not initialized"), same as test_passthrough_mode_does_not_challenge.
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient

from picx_mcp import server
from picx_mcp.settings import Settings


CHALLENGE_PATH = "/.well-known/openai-apps-challenge"
TOKEN = "openai-apps-verification-abc123XYZ_the-real-token-comes-from-the-portal"


def _settings(**overrides: object) -> Settings:
    base = dict(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        redis_url="redis://localhost:6379",
        request_state_key="x" * 32,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _app_under(settings: Settings):
    """Build the real ASGI app with get_settings patched everywhere it is read."""
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


class TestChallengeUnset:
    """When the token is unset/empty the route must 404 — the inert default."""

    def test_404_when_token_unset(self) -> None:
        """Default behaviour: no token -> 404. Matches live production today.

        The live URL 404s right now; this route must not regress that until a
        human sets OPENAI_APPS_CHALLENGE_TOKEN from the portal.
        """
        app, patches = _app_under(_settings(openai_apps_challenge_token=None))
        try:
            with TestClient(app) as client:
                resp = client.get(CHALLENGE_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 404, (
            f"{CHALLENGE_PATH} must 404 when the token is unset (the inert "
            f"default matching current production), got {resp.status_code}."
        )

    def test_404_when_token_empty_string(self) -> None:
        """An empty string is falsy and must 404 — never serve a placeholder.

        Serving an empty (or placeholder) body with 200 would be worse than a
        404: the portal could treat it as a valid-but-wrong verification attempt.
        """
        app, patches = _app_under(_settings(openai_apps_challenge_token=""))
        try:
            with TestClient(app) as client:
                resp = client.get(CHALLENGE_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 404, (
            f"{CHALLENGE_PATH} must 404 when the token is an empty string, got "
            f"{resp.status_code} — an empty/placeholder token must never be served."
        )


class TestChallengeSet:
    """When the token is set the route serves it raw as text/plain, 200."""

    def test_serves_raw_token_200(self) -> None:
        app, patches = _app_under(_settings(openai_apps_challenge_token=TOKEN))
        try:
            with TestClient(app) as client:
                resp = client.get(CHALLENGE_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200, (
            f"{CHALLENGE_PATH} must return 200 when the token is set, got "
            f"{resp.status_code}."
        )

    def test_body_is_exactly_the_raw_token_no_wrapping(self) -> None:
        """The body must be EXACTLY the token string — no JSON, no list, no HTML.

        OpenAI: "The challenge endpoint must return only that plugin's
        verification token. Do not return JSON, a list of tokens, or multiple
        tokens from the same URL."
        """
        app, patches = _app_under(_settings(openai_apps_challenge_token=TOKEN))
        try:
            with TestClient(app) as client:
                resp = client.get(CHALLENGE_PATH)
        finally:
            for p in patches:
                p.stop()

        assert resp.text == TOKEN, (
            f"Body must be exactly the raw token with no wrapping; got {resp.text!r}. "
            f"OpenAI requires only the token — no JSON, no list, no HTML."
        )
        # Guard specifically against a JSON envelope regression.
        assert not resp.text.strip().startswith("{"), (
            "Body must not be a JSON object — OpenAI rejects a JSON wrapper."
        )

    def test_content_type_is_text_plain_not_json(self) -> None:
        """Content-Type must be text/plain — never application/json."""
        app, patches = _app_under(_settings(openai_apps_challenge_token=TOKEN))
        try:
            with TestClient(app) as client:
                resp = client.get(CHALLENGE_PATH)
        finally:
            for p in patches:
                p.stop()

        content_type = resp.headers.get("content-type", "")
        assert content_type.startswith("text/plain"), (
            f"Content-Type must be text/plain, got {content_type!r} — the token "
            f"is a plain string, not JSON."
        )
        assert "application/json" not in content_type, (
            f"Content-Type must NOT be application/json, got {content_type!r}."
        )
