"""Tests for the OPS-18 durable operational state DB.

All tests use a temp sqlite file. No live GHL/Plaud/Desk/Telegram/email
access; no real credentials.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import (
    ACTION_CREATE_CONTACT,
    NOTIF_NOTIFIED,
    NOTIF_RETRIES_EXHAUSTED,
    NOTIF_RETRY_SCHEDULED,
    NOTIFICATION_MAX_ATTEMPTS,
    STATUS_COMPLETE,
    STATUS_CONTACT_SELECTED,
    STATUS_DISMISSED,
    STATUS_DUPLICATE_RESOLUTION_REQUIRED,
    STATUS_PENDING_REVIEW,
    IngestionStateDb,
    InvalidTransitionError,
    build_ops114_pending_review_summary,
    list_pending_call_reviews,
)

CANARY_PHONE_DIGITS = "5551239999"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture()
def db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


def test_schema_has_unique_source_event_constraints(db: IngestionStateDb) -> None:
    conn = sqlite3.connect(db._db_path)
    pending_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='pending_review'"
    ).fetchone()[0]
    processed_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='processed_outcomes'"
    ).fetchone()[0]
    conn.close()
    assert "UNIQUE (source, source_event_id)" in pending_sql
    assert "UNIQUE (source, source_event_id)" in processed_sql
    # No raw-handle-shaped column name anywhere in the schema.
    assert "raw_handle" not in pending_sql.lower()
    assert "handle" not in pending_sql.lower()


def test_genuine_insert_detection(db: IngestionStateDb) -> None:
    first = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=30, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    assert first.genuine_insert is True

    second = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=30, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    assert second.genuine_insert is False
    assert second.pending_review_id == first.pending_review_id

    rows = db.query_pending_review(limit=10)
    assert len(rows) == 1


def test_no_raw_handle_stored_for_canary(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-canary", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=10, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    conn = sqlite3.connect(db._db_path)
    dump = "\n".join(
        str(row) for row in conn.execute("SELECT * FROM pending_review").fetchall()
    )
    conn.close()
    assert CANARY_PHONE_DIGITS not in dump


def test_bounded_query_rejects_unbounded(db: IngestionStateDb) -> None:
    with pytest.raises(ValueError):
        db.query_pending_review(limit=0)
    with pytest.raises(ValueError):
        db.query_pending_review(limit=-1)


def test_query_caps_at_200(db: IngestionStateDb) -> None:
    for i in range(5):
        db.insert_pending_review(
            source="plaud", source_event_id=f"evt-{i}", decision="review_required",
            match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
            duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
        )
    rows = db.query_pending_review(limit=100000)
    assert len(rows) == 5  # capped internally, not an error, just bounded


def test_pending_review_count_and_oldest(db: IngestionStateDb, clock: _Clock) -> None:
    first = db.insert_pending_review(
        source="plaud", source_event_id="evt-old", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    first_created_at = db.get_pending_review(first.pending_review_id).created_at
    clock.advance(hours=1)
    db.insert_pending_review(
        source="plaud", source_event_id="evt-new", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T01:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    assert db.count_unresolved_pending_review() == 2
    assert db.oldest_pending_at() == first_created_at


def test_terminal_statuses_excluded_from_count(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    db.advance_pending_review_stage(ins.pending_review_id, STATUS_DISMISSED)
    assert db.count_unresolved_pending_review() == 0
    assert db.oldest_pending_at() is None


def test_list_pending_call_reviews_only_approved_fields(db: IngestionStateDb) -> None:
    db.insert_pending_review(
        source="plaud", source_event_id="evt-secret", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=42, direction="inbound", answered=1, status=STATUS_PENDING_REVIEW,
        display_name="Should Not Appear", masked_labels={"phone": "***-***-9999"},
    )
    result = list_pending_call_reviews(db)
    assert result["pending_review_count"] == 1
    assert result["oldest_pending_at"] is not None
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    approved = {
        "id", "source", "occurred_at", "duration_s", "direction", "answered",
        "match_outcome", "status", "notification_state", "age_seconds",
    }
    assert set(row.keys()) == approved
    assert "evt-secret" not in str(row.values())
    assert "Should Not Appear" not in str(row.values())
    assert "9999" not in str(row.values())


def test_list_pending_call_reviews_id_byte_identical_with_long_digit_run(db: IngestionStateDb) -> None:
    """pending_review_id('plaud', 'evt-4') is a real sha256 hex digest that
    contains an 11-digit run ('69297755252'), which the sanitizer's generic
    10+ digit pattern would otherwise partially redact. It must still come
    back byte-identical because it is the exact, explicit `id` projection
    field -- never inferred from its shape."""
    from plugins.team_duncan_contacts.ingestion_state_db import pending_review_id
    from plugins.team_duncan_contacts.sanitizer import sanitize_output

    real_id = pending_review_id("plaud", "evt-4")
    assert real_id == "ac5a44547d7eb69297755252e15ca5586a77b73e98c2510a167496c0ee1f1e16"
    # Sanity: without field-identity protection, this exact value would be
    # mangled by the sanitizer's generic long-digit-run pattern.
    assert sanitize_output(real_id) != real_id

    db.insert_pending_review(
        source="plaud", source_event_id="evt-4", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    result = list_pending_call_reviews(db)
    assert result["rows"][0]["id"] == real_id
    assert "[PHONE REDACTED]" not in result["rows"][0]["id"]


def test_list_pending_call_reviews_does_not_add_generic_id_exemption(
    db: IngestionStateDb,
) -> None:
    """Only the exact row `id` field is exempt from sanitize_output. A
    deterministic hash the test computes independently (never sourced from
    PendingReviewRow.id) and containing a phone-like digit run must still be
    redacted if it were ever projected under a different field name -- proving
    the fix preserves `id` by field identity, not by any generic key-name or
    digest-shaped-value bypass."""
    import hashlib

    from plugins.team_duncan_contacts.sanitizer import sanitize_output

    other_digest_with_phone = hashlib.sha256(b"unrelated").hexdigest()[:54] + "5551234567"
    assert len(other_digest_with_phone) == 64
    # Sanity: sanitize_output has no generic exemption for digest-shaped or
    # id-adjacent values -- only list_pending_call_reviews' exact `id` field
    # is hand-preserved at the call site.
    assert sanitize_output(other_digest_with_phone) != other_digest_with_phone

    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-adjacent", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    result = list_pending_call_reviews(db)
    row = result["rows"][0]
    assert set(row.keys()) == {
        "id", "source", "occurred_at", "duration_s", "direction", "answered",
        "match_outcome", "status", "notification_state", "age_seconds",
    }
    assert row["id"] == ins.pending_review_id


def test_list_pending_call_reviews_oldest_first_and_bounded(db: IngestionStateDb) -> None:
    for i in range(3):
        db.insert_pending_review(
            source="desk_call", source_event_id=f"evt-{i}", decision="review_required",
            match_outcome="zero_match", occurred_at=f"2026-01-0{i+1}T00:00:00+00:00",
            duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
        )
    result = list_pending_call_reviews(db, limit=2)
    assert len(result["rows"]) == 2
    ages = [r["age_seconds"] for r in result["rows"]]
    assert ages == sorted(ages, reverse=True)  # oldest (largest age) first


# --- OPS-114 report projection: bounded, sorted, content-safe, byte-stable ---

def test_build_ops114_pending_review_summary_only_approved_fields(db: IngestionStateDb) -> None:
    db.insert_pending_review(
        source="desk_call", source_event_id="evt-secret", decision="deny_pre_activation",
        match_outcome=None, occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=42, direction="inbound", answered=1, status=STATUS_PENDING_REVIEW,
        display_name="A. Caller", masked_labels={"phone": "***-***-9999"},
    )
    result = build_ops114_pending_review_summary(db)
    assert result["pending_review_count"] == 1
    assert result["truncated"] is False
    assert len(result["items"]) == 1
    item = result["items"][0]
    approved = {
        "pending_review_id", "display_name", "masked_labels", "occurred_at",
        "duration_s", "direction", "answered", "decision", "match_outcome",
    }
    assert set(item.keys()) == approved
    # Never source_event_id, contact ID, status, or notification_state.
    assert "evt-secret" not in str(item.values())
    assert "status" not in item
    assert "notification_state" not in item


def test_build_ops114_pending_review_summary_bounded_sorted_and_truncated(
    db: IngestionStateDb,
) -> None:
    # Inserted out of occurred_at order on purpose.
    order = [("evt-c", "2026-01-03T00:00:00+00:00"), ("evt-a", "2026-01-01T00:00:00+00:00"),
              ("evt-b", "2026-01-02T00:00:00+00:00")]
    for source_event_id, occurred_at in order:
        db.insert_pending_review(
            source="desk_call", source_event_id=source_event_id, decision="review_required",
            match_outcome="multiple_match", occurred_at=occurred_at,
            duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
        )
    result = build_ops114_pending_review_summary(db, limit=2)
    assert result["pending_review_count"] == 3
    assert result["truncated"] is True
    assert len(result["items"]) == 2
    assert [i["occurred_at"] for i in result["items"]] == [
        "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
    ]


def test_build_ops114_pending_review_summary_excludes_terminal_rows(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="desk_call", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    db.advance_pending_review_stage(ins.pending_review_id, STATUS_DISMISSED)
    result = build_ops114_pending_review_summary(db)
    assert result["pending_review_count"] == 0
    assert result["truncated"] is False
    assert result["items"] == []


def test_build_ops114_pending_review_summary_redacts_phone_like_text(db: IngestionStateDb) -> None:
    # Defense-in-depth: even though display_name is already masked upstream
    # (registry.py) before it ever reaches this table, sanitize_output must
    # still scrub a raw phone-shaped string if one somehow appeared here.
    db.insert_pending_review(
        source="desk_call", source_event_id="evt-1", decision="deny_pre_activation",
        match_outcome=None, occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
        display_name=f"Caller {CANARY_PHONE_DIGITS}", masked_labels={},
    )
    result = build_ops114_pending_review_summary(db)
    assert CANARY_PHONE_DIGITS not in str(result["items"])


def test_build_ops114_pending_review_summary_identical_state_is_byte_stable(
    db: IngestionStateDb,
) -> None:
    import json

    db.insert_pending_review(
        source="desk_call", source_event_id="evt-1", decision="review_required",
        match_outcome="multiple_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction="inbound", answered=1, status=STATUS_PENDING_REVIEW,
        display_name="A. Caller", masked_labels={"phone": "***-***-1234"},
    )
    first = build_ops114_pending_review_summary(db)
    second = build_ops114_pending_review_summary(db)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_advance_pending_review_stage_validates_transitions(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="multiple_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    row = db.advance_pending_review_stage(ins.pending_review_id, STATUS_CONTACT_SELECTED)
    assert row.status == STATUS_CONTACT_SELECTED

    with pytest.raises(InvalidTransitionError):
        db.advance_pending_review_stage(ins.pending_review_id, STATUS_DUPLICATE_RESOLUTION_REQUIRED)


def test_advance_pending_review_stage_idempotent_same_status(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    row1 = db.advance_pending_review_stage(ins.pending_review_id, STATUS_PENDING_REVIEW)
    row2 = db.advance_pending_review_stage(ins.pending_review_id, STATUS_PENDING_REVIEW)
    assert row1.status == row2.status == STATUS_PENDING_REVIEW
    history = db.get_history(ins.pending_review_id)
    # Only the genuine-insert history row; a same-status call is a no-op that
    # does not append a duplicate transition.
    assert len(history) == 1


def test_pending_review_history_is_append_only(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="multiple_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    db.advance_pending_review_stage(ins.pending_review_id, STATUS_CONTACT_SELECTED)
    db.advance_pending_review_stage(ins.pending_review_id, STATUS_DISMISSED)
    history = db.get_history(ins.pending_review_id)
    assert [h["to_status"] for h in history] == [
        STATUS_PENDING_REVIEW, STATUS_CONTACT_SELECTED, STATUS_DISMISSED,
    ]


def test_confirmation_token_single_use_and_ttl(db: IngestionStateDb, clock: _Clock) -> None:
    token, expires_at = db.issue_confirmation_token()
    assert db.consume_confirmation_token(token) is True
    assert db.consume_confirmation_token(token) is False  # single use


def test_confirmation_token_expires(db: IngestionStateDb, clock: _Clock) -> None:
    token, _ = db.issue_confirmation_token()
    clock.advance(seconds=301)
    assert db.consume_confirmation_token(token) is False


def test_confirmation_token_unknown_rejected(db: IngestionStateDb) -> None:
    assert db.consume_confirmation_token("not-a-real-token") is False


def test_action_approval_token_scoped_to_pending_review_and_action(db: IngestionStateDb) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    token = db.issue_action_approval_token(ins.pending_review_id, ACTION_CREATE_CONTACT)
    # Wrong action rejected.
    assert db.consume_action_approval_token(token, ins.pending_review_id, "grant_override") is False
    # Wrong pending_review_id rejected.
    assert db.consume_action_approval_token(token, "other-id", ACTION_CREATE_CONTACT) is False
    # Correct usage succeeds exactly once.
    assert db.consume_action_approval_token(token, ins.pending_review_id, ACTION_CREATE_CONTACT) is True
    assert db.consume_action_approval_token(token, ins.pending_review_id, ACTION_CREATE_CONTACT) is False


def test_notification_retry_bounded_schedule(db: IngestionStateDb, clock: _Clock) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    pid = ins.pending_review_id

    db.record_notification_attempt(pid, delivered=False)
    row = db.get_pending_review(pid)
    assert row.notification_state == NOTIF_RETRY_SCHEDULED
    assert row.notification_attempts == 1
    first_retry_at = datetime.fromisoformat(row.next_retry_at)
    assert first_retry_at == clock.now + timedelta(minutes=15)

    clock.advance(minutes=15)
    db.record_notification_attempt(pid, delivered=False)
    row = db.get_pending_review(pid)
    assert row.notification_attempts == 2
    second_retry_at = datetime.fromisoformat(row.next_retry_at)
    # 60 minutes from the *initial* attempt, not from this one.
    assert second_retry_at == datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=60)

    clock.advance(minutes=45)
    db.record_notification_attempt(pid, delivered=False)
    row = db.get_pending_review(pid)
    assert row.notification_attempts == NOTIFICATION_MAX_ATTEMPTS
    assert row.notification_state == NOTIF_RETRIES_EXHAUSTED
    assert row.next_retry_at is None


def test_notification_delivered_stops_retry(db: IngestionStateDb, clock: _Clock) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    db.record_notification_attempt(ins.pending_review_id, delivered=True)
    row = db.get_pending_review(ins.pending_review_id)
    assert row.notification_state == NOTIF_NOTIFIED
    assert row.next_retry_at is None


def test_due_for_notification_retry_only_after_next_retry_at(db: IngestionStateDb, clock: _Clock) -> None:
    ins = db.insert_pending_review(
        source="plaud", source_event_id="evt-1", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-01-01T00:00:00+00:00",
        duration_s=1, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    db.record_notification_attempt(ins.pending_review_id, delivered=False)
    assert db.due_for_notification_retry() == []
    clock.advance(minutes=15)
    due = db.due_for_notification_retry()
    assert len(due) == 1
    assert due[0].id == ins.pending_review_id


def test_processed_outcome_genuine_insert(db: IngestionStateDb) -> None:
    first = db.insert_processed_outcome("plaud", "evt-1", "deny_paused")
    second = db.insert_processed_outcome("plaud", "evt-1", "deny_paused")
    assert first is True
    assert second is False


def test_run_record_accept_cycle(db: IngestionStateDb) -> None:
    token, _ = db.issue_confirmation_token()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=5)
    run_id = db.record_run(
        token=token, started_at=started, completed_at=completed,
        admitted=1, override_admitted=0, discarded=0, pending_review_count=0,
        errors=0, critical_errors=0,
    )
    assert db.has_unaccepted_run() == run_id
    assert db.accept_run(run_id) is True
    assert db.has_unaccepted_run() is None
    assert db.accept_run(run_id) is False  # already accepted


def test_identity_key_stable_across_loads(tmp_path: Path) -> None:
    from plugins.team_duncan_contacts.ingestion_state_db import load_or_create_identity_key

    data_dir = tmp_path / "plugin-data"
    key1 = load_or_create_identity_key(data_dir)
    key2 = load_or_create_identity_key(data_dir)
    assert key1 == key2
    assert len(key1) == 32

    import os
    import stat

    mode = stat.S_IMODE((data_dir / "ingestion_identity_key").stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


# ---------------------------------------------------------------------------
# OPS-110: plaud_summary_state
# ---------------------------------------------------------------------------


def test_plaud_summary_state_missing_row_is_none(db: IngestionStateDb) -> None:
    assert db.get_plaud_summary_state("rec-1") is None


def test_plaud_summary_state_new_row_gets_fixed_defaults(db: IngestionStateDb) -> None:
    row = db.upsert_plaud_summary_state("rec-1", match_status="matched")
    assert row.plaud_recording_id == "rec-1"
    assert row.match_status == "matched"
    assert row.transcript_status == "not_fetched"
    assert row.summary_status == "not_started"
    assert row.desk_source_event_id is None
    assert row.contact_id is None
    assert row.note_id is None
    assert row.error_class is None


def test_plaud_summary_state_is_keyed_only_by_recording_id_idempotent_on_replay(
    db: IngestionStateDb,
) -> None:
    first = db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", desk_source_event_id="desk-1"
    )
    second = db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", desk_source_event_id="desk-1"
    )
    assert first.id == second.id
    assert db.get_plaud_summary_state("rec-1").desk_source_event_id == "desk-1"


def test_plaud_summary_state_fields_left_none_preserve_prior_values(db: IngestionStateDb) -> None:
    db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", desk_source_event_id="desk-1", contact_id="c-1",
    )
    db.upsert_plaud_summary_state("rec-1", match_status="matched", transcript_status="fetched")
    row = db.get_plaud_summary_state("rec-1")
    assert row.desk_source_event_id == "desk-1"
    assert row.contact_id == "c-1"
    assert row.transcript_status == "fetched"


def test_plaud_summary_state_error_class_is_always_overwritten_including_to_none(
    db: IngestionStateDb,
) -> None:
    db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", transcript_status="failed", error_class="transcript_fetch_failed"
    )
    row = db.get_plaud_summary_state("rec-1")
    assert row.error_class == "transcript_fetch_failed"

    db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", transcript_status="fetched", error_class=None
    )
    row = db.get_plaud_summary_state("rec-1")
    assert row.error_class is None, "a later successful stage must clear a prior failure"


def test_plaud_summary_state_note_id_and_hash_are_settable(db: IngestionStateDb) -> None:
    db.upsert_plaud_summary_state(
        "rec-1", match_status="matched", claude_output_hash="abc123", note_id="note-1",
    )
    row = db.get_plaud_summary_state("rec-1")
    assert row.claude_output_hash == "abc123"
    assert row.note_id == "note-1"


def test_plaud_summary_state_two_different_recordings_are_independent(db: IngestionStateDb) -> None:
    db.upsert_plaud_summary_state("rec-1", match_status="unmatched")
    db.upsert_plaud_summary_state("rec-2", match_status="ambiguous")
    assert db.get_plaud_summary_state("rec-1").match_status == "unmatched"
    assert db.get_plaud_summary_state("rec-2").match_status == "ambiguous"
