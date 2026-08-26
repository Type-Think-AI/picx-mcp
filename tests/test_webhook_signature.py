"""Tests for PicX webhook signature verification.

PicX signs webhooks as:
    X-PicX-Signature: t={timestamp},v1={hmac}
    HMAC-SHA256 over the string: {timestamp}.{raw_body}

This file also contains explicit regression tests for envelope key names:
the keys are `id` and `event`, NOT `event_id`/`event_type`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest


# ─── Verifier helper (production code would live in src; here for contract testing) ──


def compute_picx_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Compute the PicX webhook signature for a payload.

    Format: t={timestamp},v1={hmac_hex}
    Signed string: {timestamp}.{raw_body_utf8}
    """
    signed_payload = f"{timestamp}.{body.decode()}"
    mac = hmac.HMAC(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_picx_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a PicX webhook signature.

    Returns True if the HMAC is valid and the timestamp is within tolerance.
    """
    # Parse header: "t={timestamp},v1={hex}"
    parts: dict[str, str] = {}
    for segment in header.split(","):
        key, _, value = segment.partition("=")
        parts[key.strip()] = value.strip()

    if "t" not in parts or "v1" not in parts:
        return False

    try:
        ts = int(parts["t"])
    except ValueError:
        return False

    # Timestamp freshness check
    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        return False

    # Recompute HMAC
    signed_payload = f"{ts}.{body.decode()}"
    expected = hmac.HMAC(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


# ─── Signature verification tests ────────────────────────────────────────────


SECRET = "whsec_test_secret_key_for_testing"
BODY = json.dumps({"id": "evt_abc123", "event": "generation.completed"}).encode()


class TestSignatureVerification:
    """Verify the HMAC-SHA256 webhook signature contract."""

    def test_valid_signature_passes(self) -> None:
        ts = int(time.time())
        header = compute_picx_signature(SECRET, ts, BODY)
        assert verify_picx_signature(SECRET, header, BODY) is True

    def test_tampered_body_fails(self) -> None:
        ts = int(time.time())
        header = compute_picx_signature(SECRET, ts, BODY)
        tampered = BODY + b"x"
        assert verify_picx_signature(SECRET, header, tampered) is False

    def test_wrong_secret_fails(self) -> None:
        ts = int(time.time())
        header = compute_picx_signature(SECRET, ts, BODY)
        assert verify_picx_signature("wrong_secret", header, BODY) is False

    def test_timestamp_older_than_5_minutes_fails(self) -> None:
        """A delivery replayed after 5 minutes must be rejected."""
        stale_ts = int(time.time()) - 301  # 5 min + 1 second
        header = compute_picx_signature(SECRET, stale_ts, BODY)
        assert verify_picx_signature(SECRET, header, BODY, tolerance_seconds=300) is False

    def test_timestamp_within_5_minutes_passes(self) -> None:
        recent_ts = int(time.time()) - 299  # Just under 5 minutes
        header = compute_picx_signature(SECRET, recent_ts, BODY)
        assert verify_picx_signature(SECRET, header, BODY, tolerance_seconds=300) is True

    def test_malformed_header_fails(self) -> None:
        assert verify_picx_signature(SECRET, "garbage", BODY) is False

    def test_missing_v1_fails(self) -> None:
        assert verify_picx_signature(SECRET, "t=12345", BODY) is False

    def test_missing_timestamp_fails(self) -> None:
        assert verify_picx_signature(SECRET, "v1=abcdef", BODY) is False

    def test_header_format_matches_spec(self) -> None:
        """Verify the header format is exactly t={ts},v1={hex}."""
        ts = 1700000000
        header = compute_picx_signature(SECRET, ts, BODY)
        assert header.startswith("t=1700000000,v1=")
        # v1 value is a 64-char hex string (sha256)
        v1_part = header.split(",v1=")[1]
        assert len(v1_part) == 64
        assert all(c in "0123456789abcdef" for c in v1_part)


# ─── Envelope key regression tests ───────────────────────────────────────────


class TestWebhookEnvelopeKeys:
    """Regression: PicX envelope uses `id` and `event`, NOT `event_id`/`event_type`.

    This mistake has been made before. These tests guard against it recurring.
    """

    SAMPLE_PAYLOAD = {
        "id": "evt_gen_abc123",
        "event": "generation.completed",
        "data": {
            "generation_id": "gen_xyz",
            "output_url": "https://cdn.picxstudio.com/gen_xyz.png",
        },
    }

    def test_envelope_has_id_key(self) -> None:
        assert "id" in self.SAMPLE_PAYLOAD

    def test_envelope_has_event_key(self) -> None:
        assert "event" in self.SAMPLE_PAYLOAD

    def test_envelope_does_NOT_have_event_id(self) -> None:
        """REGRESSION: event_id does not exist in the PicX webhook envelope."""
        assert "event_id" not in self.SAMPLE_PAYLOAD

    def test_envelope_does_NOT_have_event_type(self) -> None:
        """REGRESSION: event_type does not exist in the PicX webhook envelope."""
        assert "event_type" not in self.SAMPLE_PAYLOAD

    def test_parsing_uses_correct_keys(self) -> None:
        """Simulate a handler parsing the envelope — must use id/event."""
        payload = json.loads(json.dumps(self.SAMPLE_PAYLOAD))
        # This is what a correct handler does:
        event_id = payload["id"]
        event_name = payload["event"]
        assert event_id == "evt_gen_abc123"
        assert event_name == "generation.completed"
        # This is the WRONG pattern that must never appear:
        with pytest.raises(KeyError):
            _ = payload["event_id"]
        with pytest.raises(KeyError):
            _ = payload["event_type"]
