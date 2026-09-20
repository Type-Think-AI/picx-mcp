"""The retired-grant-key 401, and the two mechanisms that close it.

## The bug these tests exist for

picx-studio's `resolve_oauth_grant_key` ROTATES: "Retire any live grant key for
this user before minting the next one." Exactly one grant key is live per user,
so a second exchange invalidates the key the first one handed out.

`context.py` therefore caches the exchanged key — that cache is correctness
machinery, not an optimisation. But it was a per-process dict, while this
deployment runs `instance_count: 2` with `stateless_http=True` (no sticky
sessions, because MCP clients use `fetch()` and drop `Set-Cookie`). So:

    call 1 -> replica A: miss -> exchange -> K1, cached on A
    call 2 -> replica B: miss (separate process) -> exchange -> K1 RETIRED, K2 on B
    call 3 -> replica A: HIT -> sends K1 -> /v1 401s -> and nothing invalidated it,
             so A kept replaying the dead key for the rest of the 60s TTL

Two mechanisms now close it, and both are tested here because each covers a case
the other cannot:

  1. A SHARED cache (`store.py`, on the Valkey already wired for the task
     backend) — replica B reads A's entry and never exchanges at all. This
     removes the common case.
  2. A one-shot RETRY on 401 (`PicXClient.on_auth_failure`) — covers the residual
     race where two replicas miss the cache at the same instant and both
     exchange. Chosen over a distributed lock: serialising every exchange would
     add a new way for the whole auth path to stall, whereas letting the loser
     notice and recover costs one extra exchange only when the race actually
     happens.

The tests simulate two replicas as two calls sharing one fake store, which is
exactly what the replicas share in production and the only thing that differs
between them for this bug.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from picx_mcp import context, store
from picx_mcp.client import PicXClient, PicXError


@pytest.fixture(autouse=True)
def _clean_local_cache():
    context.clear_session_key_cache()
    yield
    context.clear_session_key_cache()


def _token(*, token="oauth-token-abc", subject="user-uuid-1", scopes=None):
    return SimpleNamespace(token=token, subject=subject, scopes=scopes or [])


class FakeSharedStore:
    """An in-memory stand-in for the Valkey tier, shared between "replicas"."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.reads = 0
        self.writes = 0

    async def get(self, access_token: str) -> str | None:
        self.reads += 1
        return self.data.get(access_token)

    async def set(self, access_token: str, session_key: str, ttl: int) -> bool:
        self.writes += 1
        self.data[access_token] = session_key
        return True

    async def invalidate(self, access_token: str) -> None:
        self.data.pop(access_token, None)


@pytest.fixture()
def shared(monkeypatch: pytest.MonkeyPatch) -> FakeSharedStore:
    fake = FakeSharedStore()
    monkeypatch.setattr(store, "get_session_key", fake.get)
    monkeypatch.setattr(store, "set_session_key", fake.set)
    monkeypatch.setattr(store, "invalidate_session_key", fake.invalidate)
    return fake


# ─────────────────────────────────────────────────────────────────────────────
# Mechanism 1 — the shared cache stops the second replica exchanging at all
# ─────────────────────────────────────────────────────────────────────────────


class TestSharedCache:
    @pytest.mark.asyncio
    async def test_second_replica_reuses_the_first_replicas_key(
        self, monkeypatch, shared: FakeSharedStore
    ) -> None:
        """This is the whole bug, expressed as a test.

        Two processes, one shared store. The second must NOT exchange — if it
        does, it retires the key the first is still using, and the first 401s.
        Asserting on the exchange call count rather than the returned value is
        deliberate: the value would look correct either way, and the extra
        exchange is the actual defect.
        """
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(context, "_verified_access_token", lambda: _token())
        exchange = AsyncMock(return_value="pxsk_grant_K1")
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        first = await context.resolve_api_key()

        # Simulate landing on a DIFFERENT replica: same shared store, empty local
        # cache. This is the only difference between two replicas for this bug.
        context.clear_session_key_cache()
        second = await context.resolve_api_key()

        assert first == second == "pxsk_grant_K1"
        assert exchange.await_count == 1, (
            f"the second replica exchanged again ({exchange.await_count} total), "
            "which retires the key the first replica is still holding — this is "
            "the retired-key 401"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_local_cache_when_the_shared_tier_is_down(
        self, monkeypatch
    ) -> None:
        """A Valkey outage must not start failing calls.

        With no shared tier (the autouse conftest fixture latches it
        unavailable), one process must still cache locally and exchange once.
        Degrading to the old behaviour during an outage beats refusing to serve.
        """
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(context, "_verified_access_token", lambda: _token())
        exchange = AsyncMock(return_value="pxsk_grant_local")
        monkeypatch.setattr(context, "exchange_token_for_session_key", exchange)

        assert await context.resolve_api_key() == "pxsk_grant_local"
        assert await context.resolve_api_key() == "pxsk_grant_local"
        assert exchange.await_count == 1

    @pytest.mark.asyncio
    async def test_invalidation_clears_both_tiers(
        self, monkeypatch, shared: FakeSharedStore
    ) -> None:
        """A local-only delete would leave other replicas replaying a dead key."""
        monkeypatch.setattr(context, "_bearer_from_headers", lambda: None)
        monkeypatch.setattr(context, "_verified_access_token", lambda: _token())
        monkeypatch.setattr(
            context, "exchange_token_for_session_key", AsyncMock(return_value="pxsk_K1")
        )

        await context.resolve_api_key()
        assert shared.data, "nothing was written to the shared tier"

        await context.invalidate_session_key("oauth-token-abc")

        assert not shared.data, "the shared tier still holds the retired key"
        assert context._SESSION_KEY_CACHE == {}


# ─────────────────────────────────────────────────────────────────────────────
# Mechanism 2 — one-shot retry when /v1 rejects a key another replica retired
# ─────────────────────────────────────────────────────────────────────────────


def _transport(*statuses: int) -> httpx.MockTransport:
    """Respond with `statuses` in order, then 200 forever. Records auth headers."""
    seen: list[str] = []
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        status = remaining.pop(0) if remaining else 200
        if status == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, json={"detail": "Invalid API key"})

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


@pytest.fixture()
def patch_httpx(monkeypatch: pytest.MonkeyPatch):
    """Route PicXClient's httpx.AsyncClient through a MockTransport."""

    def install(transport: httpx.MockTransport):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real(*args, **kwargs)

        monkeypatch.setattr("picx_mcp.client.httpx.AsyncClient", factory)

    return install


class TestRetryOnRetiredKey:
    @pytest.mark.asyncio
    async def test_401_triggers_one_reauth_and_the_call_succeeds(
        self, patch_httpx, patch_get_settings
    ) -> None:
        """The residual race: recover instead of surfacing a spurious auth error.

        Asserts the retry actually sent the NEW credential — a retry that replays
        the stale Authorization header would loop to the same 401 and look like a
        working self-heal while fixing nothing.
        """
        transport = _transport(401)
        patch_httpx(transport)
        calls = {"n": 0}

        async def reauth() -> str:
            calls["n"] += 1
            return "pxsk_grant_K2"

        client = PicXClient(
            "pxsk_grant_K1",
            base_url="https://api.picxstudio.com/v1",
            on_auth_failure=reauth,
        )
        result = await client.get("/account/me")

        assert result == {"ok": True}
        assert calls["n"] == 1
        assert transport.seen == [  # type: ignore[attr-defined]
            "Bearer pxsk_grant_K1",
            "Bearer pxsk_grant_K2",
        ]

    @pytest.mark.asyncio
    async def test_retry_is_bounded_to_one_attempt(
        self, patch_httpx, patch_get_settings
    ) -> None:
        """A persistently-invalid credential must surface, not spin.

        Every exchange RETIRES another key, so an unbounded retry would burn
        credentials on a request that is going to fail regardless.
        """
        transport = _transport(401, 401)
        patch_httpx(transport)
        calls = {"n": 0}

        async def reauth() -> str:
            calls["n"] += 1
            return f"pxsk_grant_{calls['n']}"

        client = PicXClient(
            "pxsk_grant_K1",
            base_url="https://api.picxstudio.com/v1",
            on_auth_failure=reauth,
        )
        with pytest.raises(PicXError) as exc:
            await client.get("/account/me")

        assert exc.value.status_code == 401
        assert calls["n"] == 1, "re-authenticated more than once"

    @pytest.mark.asyncio
    async def test_no_hook_means_the_401_surfaces_untouched(
        self, patch_httpx, patch_get_settings
    ) -> None:
        """A direct pxsk_ caller gets no self-heal, by design.

        Their key is their own: a 401 means it is genuinely invalid or revoked,
        there is nothing to exchange, and retrying would just repeat a rejection.
        """
        transport = _transport(401)
        patch_httpx(transport)

        client = PicXClient("pxsk_user_key", base_url="https://api.picxstudio.com/v1")
        with pytest.raises(PicXError) as exc:
            await client.get("/account/me")

        assert exc.value.status_code == 401
        assert len(transport.seen) == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_failed_reauth_surfaces_the_original_401(
        self, patch_httpx, patch_get_settings
    ) -> None:
        """If the exchange itself is broken, report the 401 the caller actually hit.

        Replacing it with the exchange's own error would point the user at a
        server misconfiguration they cannot act on, and hide the credential
        rejection that is the real symptom.
        """
        transport = _transport(401)
        patch_httpx(transport)

        async def reauth() -> None:
            return None

        client = PicXClient(
            "pxsk_grant_K1",
            base_url="https://api.picxstudio.com/v1",
            on_auth_failure=reauth,
        )
        with pytest.raises(PicXError) as exc:
            await client.get("/account/me")

        assert exc.value.status_code == 401
        assert len(transport.seen) == 1  # type: ignore[attr-defined]


class TestStoreKeyHygiene:
    def test_cache_key_never_contains_the_raw_token(self) -> None:
        """Redis keys show up in MONITOR/SLOWLOG/KEYS — a token there is a leak."""
        token = "super-secret-bearer-token-value"
        key = store._key(token)

        assert token not in key
        assert key.startswith("picx-mcp:sk:")
        # SHA-256 hex, so the key is fixed-length and reveals nothing.
        assert len(key) == len("picx-mcp:sk:") + 64

    def test_distinct_tokens_get_distinct_keys(self) -> None:
        assert store._key("token-a") != store._key("token-b")
