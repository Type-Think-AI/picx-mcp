"""The OAuth token -> PicX session-key exchange, wired into `resolve_api_key`.

Before this wiring, a request carrying a VALID OAuth access token — one that
already passed the 401 challenge and the JWTVerifier's signature/iss/aud
checks — still could not complete a tool call: `resolve_api_key` raised 501
for anything that wasn't a raw `pxsk_`, regardless of whether it had a real
verified token or not. `test_oauth_401_challenge.py` proves the challenge
fires; this file proves what happens on the other side of it.

Three things are asserted that are each independently load-bearing:

1. A verified access token with a `subject` claim is exchanged for a session
   key via `exchange_token_for_session_key`, and that key is what
   `get_client()` is built with.
2. The exchange is CACHED per OAuth token for a bounded TTL, because the
   exchange rotates the user's PicX grant key on the API side — re-exchanging
   on every tool call in one turn would invalidate the key a preceding call in
   the SAME turn is still using.
3. A bearer token that isn't a real verified OAuth token (no auth provider
   configured, so nothing populated `subject`) is refused with 501, distinct
   from "no credential at all" (401) — the caller needs to know which is true.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from picx_mcp import context
from picx_mcp.client import PicXError


@pytest.fixture(autouse=True)
def clean_cache():
    context.clear_session_key_cache()
    yield
    context.clear_session_key_cache()


def _fake_access_token(*, token="oauth-token-abc", subject="google-sub-1", scopes=None):
    return SimpleNamespace(token=token, subject=subject, scopes=scopes or [])


class TestPxskPassthroughUnaffected:
    """A raw pxsk_ key must still bypass the exchange entirely."""

    @pytest.mark.asyncio
    async def test_pxsk_key_returned_as_is_no_exchange_attempted(self, monkeypatch):
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: "pxsk_live_abc123")
        exchange = AsyncMock()
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        result = await context.resolve_api_key()

        assert result == "pxsk_live_abc123"
        exchange.assert_not_awaited()


class TestOAuthExchange:
    """A verified OAuth token must be exchanged for a real PicX session key."""

    @pytest.mark.asyncio
    async def test_verified_token_is_exchanged_and_key_is_returned(self, monkeypatch):
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(
            context, "_verified_access_token", lambda: _fake_access_token()
        )
        exchange = AsyncMock(return_value="pxsk_grant_xyz")
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        result = await context.resolve_api_key()

        assert result == "pxsk_grant_xyz"
        exchange.assert_awaited_once()
        # The verified `subject` claim is what gets exchanged — never the raw
        # JWT string, and never anything derived from headers.
        called_subject = exchange.await_args.args[0]
        assert called_subject == "google-sub-1"

    @pytest.mark.asyncio
    async def test_granted_scopes_are_forwarded_to_the_exchange(self, monkeypatch):
        """The AS's granted scopes narrow the exchange; they must not be dropped.

        Dropping them would let `exchange_token_for_session_key`'s default (the
        full session-key scope set) silently widen a grant the user only
        approved a subset of on the consent screen.
        """
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(
            context,
            "_verified_access_token",
            lambda: _fake_access_token(scopes=["images:generate"]),
        )
        exchange = AsyncMock(return_value="pxsk_grant_xyz")
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        await context.resolve_api_key()

        _, kwargs = exchange.await_args
        assert kwargs.get("scopes") == ["images:generate"]

    @pytest.mark.asyncio
    async def test_get_client_awaits_resolve_api_key(self, monkeypatch):
        """`get_client` is async now — confirm it actually awaits, not returns a coroutine."""
        monkeypatch.setattr(context, "resolve_api_key", AsyncMock(return_value="pxsk_from_oauth"))

        client = await context.get_client()

        assert client.api_key == "pxsk_from_oauth"


class TestSessionKeyCache:
    """The exchange must not re-run per tool call within one OAuth token's life."""

    @pytest.mark.asyncio
    async def test_second_call_with_same_token_does_not_re_exchange(self, monkeypatch):
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(
            context, "_verified_access_token", lambda: _fake_access_token(token="tok-1")
        )
        exchange = AsyncMock(return_value="pxsk_grant_1")
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        first = await context.resolve_api_key()
        second = await context.resolve_api_key()

        assert first == second == "pxsk_grant_1"
        exchange.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_different_oauth_token_gets_its_own_exchange(self, monkeypatch):
        """Two different tokens must never share a cached credential."""
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        exchange = AsyncMock(side_effect=["pxsk_for_user_a", "pxsk_for_user_b"])
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        monkeypatch.setattr(
            context, "_verified_access_token", lambda: _fake_access_token(token="tok-a", subject="sub-a")
        )
        first = await context.resolve_api_key()

        monkeypatch.setattr(
            context, "_verified_access_token", lambda: _fake_access_token(token="tok-b", subject="sub-b")
        )
        second = await context.resolve_api_key()

        assert first == "pxsk_for_user_a"
        assert second == "pxsk_for_user_b"
        assert exchange.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_expires_after_its_ttl(self, monkeypatch):
        """Verified via the module's own clock read, not by sleeping in a test."""
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(
            context, "_verified_access_token", lambda: _fake_access_token(token="tok-1")
        )
        exchange = AsyncMock(side_effect=["pxsk_first", "pxsk_second"])
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        clock = {"now": 1000.0}
        monkeypatch.setattr(context.time, "monotonic", lambda: clock["now"])

        first = await context.resolve_api_key()
        clock["now"] += context._SESSION_KEY_CACHE_TTL_SECONDS + 1
        second = await context.resolve_api_key()

        assert first == "pxsk_first"
        assert second == "pxsk_second"
        assert exchange.await_count == 2


class TestUnverifiedBearerToken:
    """A bearer token present but never verified (no auth provider configured).

    This is the "OAuth isn't enabled here" case: FastMCP's auth middleware never
    ran because build_auth() returned None, so nothing populated `subject`. It
    must read as 501 (actionable: turn on OAuth or send a pxsk_), not as the
    generic 401 used for no-credential-at-all.
    """

    @pytest.mark.asyncio
    async def test_raw_bearer_with_no_verified_subject_is_501(self, monkeypatch):
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: "some.jwt.token")
        monkeypatch.setattr(context, "_verified_access_token", lambda: None)
        exchange = AsyncMock()
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        with pytest.raises(PicXError) as caught:
            await context.resolve_api_key()

        assert caught.value.status_code == 501
        exchange.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_credential_at_all_is_401(self, monkeypatch):
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(context, "_verified_access_token", lambda: None)

        with pytest.raises(PicXError) as caught:
            await context.resolve_api_key()

        assert caught.value.status_code == 401
