"""Scope enforcement (Task 5) and credit-ceiling enforcement (Task 6.2).

`require_scope` is a no-op for the two cases already handled elsewhere:
direct `pxsk_` passthrough (checked by `/v1` itself) and no verified OAuth
token at all (resolve_api_key's own 501 covers that). It only fires when an
OAuth grant's own `access_token.scopes` visibly lacks what the tool needs.

`check_ceiling` / `record_spend` / `record_spend_once` are a simple in-process
ledger keyed by the resolved credential string — tested directly against the
module functions rather than through a live tool call, since every tool
already has its own contract tests for the request/response shape.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from picx_mcp import quota
from picx_mcp.client import PicXError


@pytest.fixture(autouse=True)
def clean_state():
    quota.clear_ledger()
    yield
    quota.clear_ledger()


def _fake_access_token(*, scopes=None):
    return SimpleNamespace(token="tok", subject="sub-1", scopes=scopes or [])


class TestRequireScope:
    def test_unknown_tool_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(quota, "_verified_access_token", lambda: _fake_access_token(scopes=[]))
        quota.require_scope("picx_get_account")  # no entry in TOOL_SCOPES — must not raise

    def test_no_verified_token_is_a_noop(self, monkeypatch):
        """Covers both pxsk_ passthrough and 'OAuth not configured' — neither is
        this function's job to police."""
        monkeypatch.setattr(quota, "_verified_access_token", lambda: None)
        quota.require_scope("picx_generate_image")  # must not raise

    def test_granted_scope_present_passes(self, monkeypatch):
        monkeypatch.setattr(
            quota, "_verified_access_token", lambda: _fake_access_token(scopes=["images:generate"])
        )
        quota.require_scope("picx_generate_image")  # must not raise

    def test_missing_scope_raises_403_before_any_side_effect(self, monkeypatch):
        monkeypatch.setattr(
            quota, "_verified_access_token", lambda: _fake_access_token(scopes=["videos:generate"])
        )
        with pytest.raises(PicXError) as caught:
            quota.require_scope("picx_generate_image")
        assert caught.value.status_code == 403
        assert "images:generate" in str(caught.value)

    @pytest.mark.parametrize(
        ("tool_name", "scope"),
        [
            ("picx_generate_image", "images:generate"),
            ("picx_edit_image", "images:edit"),
            ("picx_generate_video", "videos:generate"),
            ("picx_upload_asset", "uploads:write"),
        ],
    )
    def test_tool_scope_map_matches_session_key_scopes(self, tool_name, scope):
        """Mirrors SESSION_KEY_SCOPES in picx-studio's key_service.py exactly —
        a drift here would silently under- or over-enforce relative to /v1."""
        assert quota.TOOL_SCOPES[tool_name] == scope


class TestCheckCeiling:
    def test_under_ceiling_passes(self, monkeypatch):
        monkeypatch.setattr(
            quota, "get_settings", lambda: SimpleNamespace(session_credit_ceiling=2000)
        )
        quota.check_ceiling("pxsk_a")  # must not raise

    def test_already_at_ceiling_refuses(self, monkeypatch):
        monkeypatch.setattr(
            quota, "get_settings", lambda: SimpleNamespace(session_credit_ceiling=100)
        )
        quota.record_spend("pxsk_a", 100)
        with pytest.raises(PicXError) as caught:
            quota.check_ceiling("pxsk_a")
        assert caught.value.status_code == 402

    def test_estimate_would_exceed_ceiling_refuses_preemptively(self, monkeypatch):
        monkeypatch.setattr(
            quota, "get_settings", lambda: SimpleNamespace(session_credit_ceiling=100)
        )
        quota.record_spend("pxsk_a", 60)
        with pytest.raises(PicXError) as caught:
            quota.check_ceiling("pxsk_a", estimated_credits=50)
        assert caught.value.status_code == 402

    def test_estimate_within_remaining_ceiling_passes(self, monkeypatch):
        monkeypatch.setattr(
            quota, "get_settings", lambda: SimpleNamespace(session_credit_ceiling=100)
        )
        quota.record_spend("pxsk_a", 60)
        quota.check_ceiling("pxsk_a", estimated_credits=40)  # exactly at ceiling — must not raise

    def test_different_credentials_have_independent_ledgers(self, monkeypatch):
        monkeypatch.setattr(
            quota, "get_settings", lambda: SimpleNamespace(session_credit_ceiling=100)
        )
        quota.record_spend("pxsk_a", 100)
        quota.check_ceiling("pxsk_b")  # a different credential's ledger — must not raise


class TestRecordSpend:
    def test_zero_or_negative_spend_is_ignored(self):
        quota.record_spend("pxsk_a", 0)
        quota.record_spend("pxsk_a", -5)
        assert quota.spent_so_far("pxsk_a") == 0

    def test_spend_accumulates(self):
        quota.record_spend("pxsk_a", 10)
        quota.record_spend("pxsk_a", 15)
        assert quota.spent_so_far("pxsk_a") == 25

    def test_record_spend_once_is_idempotent_per_generation_id(self):
        quota.record_spend_once("gen-1", "pxsk_a", 40)
        quota.record_spend_once("gen-1", "pxsk_a", 40)  # a re-poll of the same generation
        assert quota.spent_so_far("pxsk_a") == 40

    def test_record_spend_once_distinct_generations_both_count(self):
        quota.record_spend_once("gen-1", "pxsk_a", 40)
        quota.record_spend_once("gen-2", "pxsk_a", 25)
        assert quota.spent_so_far("pxsk_a") == 65
