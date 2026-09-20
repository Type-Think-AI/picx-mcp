"""Per-tool scope enforcement (Task 5) and per-grant credit-ceiling enforcement
(Task 6.2) for the PicX MCP connector.

## Scope enforcement

`TOOL_SCOPES` maps a spending tool's name to the API-key scope it requires,
mirroring `SESSION_KEY_SCOPES` on the picx-studio side exactly (see
`app/api_platform/key_service.py` — that list is the source of truth; this one
must never drift from it). Enforced only in the OAuth path: a direct `pxsk_`
key is already scope-checked by `/v1` itself (e.g. `public_api/images.py`
checks `images:generate` before pricing), so re-checking it here would just
duplicate that check for no benefit. An OAuth-exchanged session key is ALSO
scope-checked by `/v1` (the exchange intersects the grant's approved scopes
into the minted key — see `resolve_oauth_grant_key` — and `/v1` still checks
on top of that), so this local check does not replace `/v1`'s enforcement. It
exists to reject BEFORE the network round trip for the one case this connector
can already see without calling anything: an access token whose own granted
scopes (from the consent screen) are visibly insufficient for the tool being
called.

## Credit ceiling

`settings.session_credit_ceiling` bounds how much credit ONE resolved PicX
credential (the `pxsk_` used for a request — a direct API key or an OAuth
grant's exchanged session key) may spend through this connector before it
refuses further spending calls. It exists independently of the account's own
daily credit cap to bound a prompt-injected drain: an over-eager or
compromised agent loop can otherwise run up to the account's full daily cap
through a SINGLE grant.

Tracked in-process, keyed by the resolved credential string, with no reset
short of the credential itself rotating (a fresh OAuth exchange or a new
`pxsk_` is a new dict key by construction) or a process restart. Like the
session-key cache in `context.py`, this is correct on a single replica; a
multi-replica deployment needs a shared store (Redis — already a soft
dependency via `settings.redis_url`) to enforce ONE ceiling across replicas
instead of effectively N x the ceiling across N replicas. That gap is
documented here rather than silently wrong, and deliberately not built in this
pass: it would add a hard Redis dependency to the common single-replica case
to close a gap that only matters once this connector scales out.
"""

from __future__ import annotations

from .client import PicXError
from .context import _verified_access_token
from .settings import get_settings

# Mirrors `SESSION_KEY_SCOPES` in picx-studio's `app/api_platform/key_service.py`.
# Read-only tools carry no entry here and are never scope-checked — matching
# `/v1`, which only checks scope on these four write routes.
TOOL_SCOPES: dict[str, str] = {
    "picx_generate_image": "images:generate",
    "picx_edit_image": "images:edit",
    "picx_generate_video": "videos:generate",
    "picx_upload_asset": "uploads:write",
}


def require_scope(tool_name: str) -> None:
    """Reject before any side effect when the caller's OAuth grant lacks the
    tool's required scope.

    No-op for direct `pxsk_` passthrough and no-op when OAuth isn't configured
    at all (both are enforced or handled elsewhere — see module docstring) and
    no-op when `tool_name` carries no entry in `TOOL_SCOPES` (nothing to
    enforce for a read-only tool).
    """
    required = TOOL_SCOPES.get(tool_name)
    if required is None:
        return
    access_token = _verified_access_token()
    if access_token is None:
        # Either a direct pxsk_ (already scope-checked by /v1) or OAuth isn't
        # configured on this deployment at all (resolve_api_key raises 501 for
        # that case on its own, downstream of this check).
        return
    granted = list(getattr(access_token, "scopes", None) or [])
    if required not in granted:
        raise PicXError(
            f"This grant does not include the '{required}' scope required by "
            f"{tool_name}. Re-authorize to get a grant covering all PicX "
            "capabilities.",
            status_code=403,
            # Carries `_meta["mcp/www_authenticate"]` on the error result so
            # ChatGPT offers re-linking instead of a dead end. This is the one
            # failure a user CAN fix, and the one grants issued before
            # DEFAULT_SCOPES became the full set are stuck on.
            oauth_challenge="insufficient_scope",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Credit ceiling
# ─────────────────────────────────────────────────────────────────────────────

_ledger: dict[str, int] = {}
_recorded_generations: set[str] = set()


def clear_ledger() -> None:
    """Drop every tracked grant's spend and recorded-generation set. Tests only."""
    _ledger.clear()
    _recorded_generations.clear()


def spent_so_far(api_key: str) -> int:
    return _ledger.get(api_key, 0)


def check_ceiling(api_key: str, *, estimated_credits: int | None = None) -> None:
    """Refuse a spending call before it runs.

    Two cases:
      - The grant has ALREADY reached the ceiling from prior calls — refused
        regardless of whether this call's cost is known.
      - `estimated_credits` is known and adding it would exceed the ceiling —
        refused pre-emptively. When no estimate is available (video has no
        local cost model), this case cannot fire; only the already-reached
        case protects a video call, which is why every spending call also
        records its actual cost afterward (`record_spend` / `record_spend_once`)
        so the NEXT call sees an accurate running total.
    """
    settings = get_settings()
    ceiling = settings.session_credit_ceiling
    spent = _ledger.get(api_key, 0)
    if spent >= ceiling:
        raise PicXError(
            f"This grant has reached its {ceiling}-credit session ceiling "
            f"({spent} spent). Re-authorize to start a new grant, or use a "
            "direct PicX API key for unbounded use.",
            status_code=402,
        )
    if estimated_credits is not None and spent + estimated_credits > ceiling:
        remaining = ceiling - spent
        raise PicXError(
            f"This call would cost approximately {estimated_credits} credits, "
            f"exceeding the grant's {ceiling}-credit session ceiling ({spent} "
            f"already spent, {remaining} remaining). Re-authorize to start a "
            "new grant, or use a direct PicX API key for unbounded use.",
            status_code=402,
        )


def record_spend(api_key: str, credits_used: int) -> None:
    """Add actual spend to the ledger after a call completes.

    Call with the API's own reported `credits_used` (never a locally-estimated
    figure) exactly once per synchronous generate/edit call, so the ledger
    reflects reality even when an estimate was wrong, unknown, or absent.
    """
    if credits_used <= 0:
        return
    _ledger[api_key] = _ledger.get(api_key, 0) + credits_used


def record_spend_once(generation_id: str, api_key: str, credits_used: int) -> None:
    """Same as `record_spend`, but idempotent per `generation_id`.

    Video generation returns its cost only when POLLED (the immediate 202 from
    `picx_generate_video` carries no `credits_used`), and a caller may poll the
    same generation many times before it reaches a terminal state. Without this
    guard, re-polling a completed generation would re-add its cost to the
    ledger on every poll. Keyed on generation_id alone (not also api_key) —
    a generation belongs to exactly one grant, so that's sufficient and keeps
    the "already recorded" set from growing unboundedly per-credential.
    """
    if generation_id in _recorded_generations:
        return
    _recorded_generations.add(generation_id)
    record_spend(api_key, credits_used)
