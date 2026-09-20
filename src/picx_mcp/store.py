"""Cross-replica shared state for the OAuth session-key exchange.

## Why this module exists

`context.py` caches the PicX session key it gets back from the token exchange.
That cache is not an optimisation — it is load-bearing for correctness, because
picx-studio's `resolve_oauth_grant_key` ROTATES on every call:

    "Retire any live grant key for this user before minting the next one."
    — api/app/api_platform/key_service.py

Exactly one grant key is live per user, so exchanging twice invalidates the key
the first exchange handed out. The cache exists so a chat turn that calls three
tools performs one exchange instead of three.

Held in a per-process dict, that cache was WRONG on this deployment, and wrong
in a way that looks like flakiness rather than breakage:

    call 1 -> replica A: miss -> exchange -> K1, cached on A
    call 2 -> replica B: miss (separate process!) -> exchange -> K1 RETIRED, K2 cached on B
    call 3 -> replica A: HIT -> sends K1 -> /v1 rejects it (is_active == False) -> 401

`.do/app.yaml` runs `instance_count: 2` and `stateless_http=True` deliberately
disables sticky sessions (MCP clients use `fetch()` and do not forward
`Set-Cookie`), so consecutive calls in ONE turn genuinely land on different
replicas. Nothing invalidated the stale entry either, so replica A kept serving
the retired key for the rest of the 60-second TTL.

This is the same failure shape as two bugs already fixed on this deployment —
OAuth codes in per-process memory behind a full `noeviction` Valkey, and an
in-memory task backend across two replicas. The lesson each time: state that is
global to a USER cannot live in memory that is local to a PROCESS.

## Why a credential is written to Valkey, and why that is acceptable

The cached value is a real `pxsk_` session key, so this puts a credential in a
shared store. Weighed deliberately:

  • It is scoped (four scopes, never wider), revocable, and expires on its own.
  • The cache TTL is 60 seconds — long enough to cover one chat turn, far too
    short to be worth harvesting.
  • DigitalOcean managed Valkey is reachable only on the project's private
    network and its `DATABASE_URL` is `rediss://`, so it is TLS in transit.
  • The alternative is not "no credential anywhere" — it is the correctness bug
    above, plus the same credential sitting in each replica's heap regardless.

The cache KEY is a SHA-256 of the access token, never the token itself: Redis
keys surface in `MONITOR`, `SLOWLOG` and `KEYS` output, and a bearer token in a
key name would be a credential leak by a different route.

## Failure posture: degrade, never block

Every function here swallows Redis errors and reports "not available" rather
than raising. A Valkey blip must not take tool calls down, and the caller keeps
a per-process fallback plus a retry-on-401 self-heal, so the worst case is a
return to the old behaviour for the duration of the outage — not an outage of
our own.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from .settings import get_settings

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

#: Namespace for every key this module writes, so the shared Valkey instance
#: (also used by the tasks docket) never collides and keys are greppable.
_PREFIX = "picx-mcp:sk:"

_client: "Redis | None" = None
_unavailable = False


def _key(access_token: str) -> str:
    """Namespaced SHA-256 of the token. Never the token itself — see module docs."""
    return _PREFIX + hashlib.sha256(access_token.encode()).hexdigest()


async def _redis() -> "Redis | None":
    """A lazily-created client, or None when Redis is unconfigured/unreachable.

    Created lazily rather than at import so a module import never performs I/O,
    and cached so we do not build a connection pool per request. `_unavailable`
    latches a hard failure (unset URL, bad scheme, missing driver) so we stop
    retrying something that cannot start working without a redeploy.
    """
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is not None:
        return _client
    url = get_settings().redis_url
    if not url:
        logger.warning(
            "redis_url is unset — the session-key cache is per-process. On a "
            "multi-replica deployment this reintroduces the retired-key 401. "
            "The retry-on-401 self-heal still covers it, at the cost of an "
            "extra exchange."
        )
        _unavailable = True
        return None
    try:
        from redis.asyncio import Redis

        _client = Redis.from_url(url, decode_responses=True)
    except Exception as exc:  # pragma: no cover — driver/url problems only
        logger.warning("session-key cache unavailable (%r); using per-process cache", exc)
        _unavailable = True
        return None
    return _client


async def get_session_key(access_token: str) -> str | None:
    """The cached session key for this access token, or None on miss/outage."""
    client = await _redis()
    if client is None:
        return None
    try:
        return await client.get(_key(access_token))
    except Exception as exc:
        logger.warning("session-key cache read failed (%r); treating as a miss", exc)
        return None


async def set_session_key(access_token: str, session_key: str, ttl_seconds: int) -> bool:
    """Cache `session_key` under this token for `ttl_seconds`. True if stored.

    The TTL is set in the same call as the value (`ex=`), not afterwards: a
    separate EXPIRE can fail on its own and would leave a live credential in the
    store with no expiry at all.
    """
    client = await _redis()
    if client is None:
        return False
    try:
        await client.set(_key(access_token), session_key, ex=ttl_seconds)
        return True
    except Exception as exc:
        logger.warning("session-key cache write failed (%r); continuing uncached", exc)
        return False


async def invalidate_session_key(access_token: str) -> None:
    """Drop the cached key for this token, across every replica.

    Called when `/v1` rejects a cached key — which means another replica retired
    it — so the next call re-exchanges instead of replaying a dead credential.
    Deleting centrally is the point: a purely local delete would leave the other
    replicas serving the same dead key until their own TTL expired.
    """
    client = await _redis()
    if client is None:
        return
    try:
        await client.delete(_key(access_token))
    except Exception as exc:
        logger.warning("session-key cache invalidation failed (%r)", exc)


async def reset_for_tests() -> None:
    """Drop the cached client and the unavailable latch. Tests only."""
    global _client, _unavailable
    client, _client, _unavailable = _client, None, False
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass
