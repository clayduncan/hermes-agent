"""Tests for OPS-18 notification payload builders and the Telegram-primary,
email-fallback notifier. No real Telegram or email send anywhere.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from plugins.team_duncan_contacts.ingestion_state_db import PendingReviewRow
from plugins.team_duncan_contacts.notifications import (
    TelegramPrimaryEmailFallbackNotifier,
    build_collision_payload,
    build_deny_pre_activation_payload,
    build_multiple_match_payload,
    build_plaud_zero_match_payload,
)


class _RecordingNotifier:
    def __init__(self, deliver: bool) -> None:
        self.deliver = deliver
        self.calls = 0

    def send(self, payload) -> bool:
        self.calls += 1
        return self.deliver


def _row(**overrides) -> PendingReviewRow:
    base = dict(
        id="pid-1", source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=30, direction=None, answered=None, status="pending_review",
        resolved_contact_id=None, idempotency_key=None, failure_stage=None,
        failure_detail=None, notification_state="not_notified", notification_attempts=0,
        last_notified_at=None, next_retry_at=None, created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00", display_name=None, masked_labels={},
    )
    base.update(overrides)
    return PendingReviewRow(**base)


def test_telegram_primary_used_when_it_succeeds() -> None:
    telegram = _RecordingNotifier(deliver=True)
    email = _RecordingNotifier(deliver=True)
    notifier = TelegramPrimaryEmailFallbackNotifier(telegram, email)
    assert notifier.send({"x": 1}) is True
    assert telegram.calls == 1
    assert email.calls == 0  # never touched when Telegram succeeds


def test_email_fallback_only_after_telegram_fails() -> None:
    telegram = _RecordingNotifier(deliver=False)
    email = _RecordingNotifier(deliver=True)
    notifier = TelegramPrimaryEmailFallbackNotifier(telegram, email)
    assert notifier.send({"x": 1}) is True
    assert telegram.calls == 1
    assert email.calls == 1


def test_both_fail_reports_failure() -> None:
    telegram = _RecordingNotifier(deliver=False)
    email = _RecordingNotifier(deliver=False)
    notifier = TelegramPrimaryEmailFallbackNotifier(telegram, email)
    assert notifier.send({"x": 1}) is False


# --- Payload projections carry only approved fields --------------------------

def test_deny_pre_activation_payload_fields() -> None:
    row = _row(decision="deny_pre_activation", display_name="A A", masked_labels={"phone": "***-***-1234"})
    payload = build_deny_pre_activation_payload(row)
    assert set(payload.keys()) == {
        "source", "occurred_at", "duration_s", "direction", "display_name",
        "masked_labels", "prompt", "pending_review_id",
    }


def test_plaud_zero_match_payload_fields() -> None:
    row = _row()
    payload = build_plaud_zero_match_payload(row, "***-***-9999")
    assert set(payload.keys()) == {
        "source", "occurred_at", "duration_s", "direction", "masked_source_label",
        "prompt", "pending_review_id",
    }
    assert payload["masked_source_label"] == "***-***-9999"


def test_multiple_match_payload_carries_only_token_and_label() -> None:
    row = _row(match_outcome="multiple_match")
    candidates = [
        {"selection_token": "tok-a", "display_label": "Alice|****1111|(none)|(none)|(none)", "contact_id": "should-not-leak"},
    ]
    payload = build_multiple_match_payload(row, candidates)
    assert payload["candidates"] == [{"selection_token": "tok-a", "display_label": "Alice|****1111|(none)|(none)|(none)"}]
    assert "contact_id" not in str(payload)
    assert "should-not-leak" not in str(payload)


def test_collision_payload_has_no_candidate_data() -> None:
    row = _row(match_outcome="multiple_match")
    payload = build_collision_payload(row)
    assert "candidates" not in payload
    assert set(payload.keys()) == {
        "source", "occurred_at", "duration_s", "direction", "prompt", "pending_review_id",
    }


# --- Adversarial: exempt identifiers survive; everything adjacent still redacted --

# A real sha256 hex digest that happens to contain a long run of decimal
# digits -- exactly the shape that previously triggered partial redaction.
_DIGIT_RUN_ID = hashlib.sha256(b"pending-review-canary").hexdigest()[:44] + "5551234567890"
_DIGIT_RUN_TOKEN = hashlib.sha256(b"selection-token-canary").hexdigest()[:40] + "18005551234567"

# A value with the exact 64-hex-char shape of a sha256 digest, but which is
# NOT an identifier field -- it must still be sanitized because the
# correction is scoped by field identity, never by content shape.
_DIGEST_SHAPED_NON_IDENTIFIER = hashlib.sha256(b"not-an-id").hexdigest()[:54] + "5559876543"


def test_pending_review_id_byte_identical_across_every_notification_payload_type() -> None:
    row = _row(id=_DIGIT_RUN_ID)

    deny_payload = build_deny_pre_activation_payload(row)
    assert deny_payload["pending_review_id"] == _DIGIT_RUN_ID

    zero_match_payload = build_plaud_zero_match_payload(row, None)
    assert zero_match_payload["pending_review_id"] == _DIGIT_RUN_ID

    multi_payload = build_multiple_match_payload(
        row, [{"selection_token": "tok-a", "display_label": "Alice"}]
    )
    assert multi_payload["pending_review_id"] == _DIGIT_RUN_ID

    collision_payload = build_collision_payload(row)
    assert collision_payload["pending_review_id"] == _DIGIT_RUN_ID

    for payload in (deny_payload, zero_match_payload, multi_payload, collision_payload):
        assert "[PHONE REDACTED]" not in payload["pending_review_id"]


def test_selection_token_byte_identical_with_long_digit_run() -> None:
    row = _row(match_outcome="multiple_match")
    candidates = [{"selection_token": _DIGIT_RUN_TOKEN, "display_label": "Alice"}]
    payload = build_multiple_match_payload(row, candidates)
    assert payload["candidates"][0]["selection_token"] == _DIGIT_RUN_TOKEN
    assert "[PHONE REDACTED]" not in payload["candidates"][0]["selection_token"]


def test_adjacent_phone_like_strings_still_redacted_in_every_non_exempt_field() -> None:
    """A deterministic long-digit-run id/token sitting right next to genuine
    raw phone-like content must not shield that adjacent content: only the
    exact exempt fields skip sanitize_output, everything else still runs
    through it in full."""
    row = _row(
        id=_DIGIT_RUN_ID,
        display_name="555-123-9999",
        masked_labels={"phone": "***-***-1234", "raw_adjacent": "(555) 123-4567"},
    )
    deny_payload = build_deny_pre_activation_payload(row)
    assert deny_payload["pending_review_id"] == _DIGIT_RUN_ID
    assert deny_payload["display_name"] == "[PHONE REDACTED]"
    assert deny_payload["masked_labels"]["raw_adjacent"] == "[PHONE REDACTED]"

    zero_match_payload = build_plaud_zero_match_payload(row, "555-987-6543")
    assert zero_match_payload["pending_review_id"] == _DIGIT_RUN_ID
    assert zero_match_payload["masked_source_label"] == "[PHONE REDACTED]"

    candidates = [
        {"selection_token": _DIGIT_RUN_TOKEN, "display_label": "Alice 555-111-2222"},
    ]
    multi_payload = build_multiple_match_payload(row, candidates)
    assert multi_payload["pending_review_id"] == _DIGIT_RUN_ID
    assert multi_payload["candidates"][0]["selection_token"] == _DIGIT_RUN_TOKEN
    assert multi_payload["candidates"][0]["display_label"] == "Alice [PHONE REDACTED]"


def test_digest_shaped_non_identifier_field_is_still_sanitized() -> None:
    """A field that is NOT one of the exact exempt identifier fields, but
    whose value has the exact 64-hex-char shape of a sha256 digest, must
    still be redacted if it carries a phone-like digit run. Proves the fix
    is scoped by field identity, not by any exemption for opaque-looking
    strings."""
    row = _row(
        id=_DIGIT_RUN_ID,
        display_name="A A",
        masked_labels={"suspicious_digest_shaped_value": _DIGEST_SHAPED_NON_IDENTIFIER},
    )
    payload = build_deny_pre_activation_payload(row)
    assert payload["pending_review_id"] == _DIGIT_RUN_ID  # exempt field untouched
    redacted = payload["masked_labels"]["suspicious_digest_shaped_value"]
    assert redacted != _DIGEST_SHAPED_NON_IDENTIFIER
    assert "5559876543" not in redacted


def test_collision_payload_carries_no_candidates_or_selection_tokens_even_with_digit_run_id() -> None:
    row = _row(id=_DIGIT_RUN_ID, match_outcome="multiple_match")
    payload = build_collision_payload(row)
    assert "candidates" not in payload
    assert "selection_token" not in str(payload)
    assert payload["pending_review_id"] == _DIGIT_RUN_ID
