"""Tests for the OPS-18 GHL note mirror (plugins/team_duncan_contacts/note_mirror.py).

Uses a fake GHL client (no live network) and the real IngestionStateDb
against a temp sqlite file. Covers: note body privacy, marker-based crash
recovery, no-duplicate-on-retry, and the create/read-back contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import (
    NoteMirror,
    contact_detail_url,
    format_note_body,
    note_marker_for_event,
)

OCCURRED_AT = datetime(2026, 9, 21, 20, 14, tzinfo=timezone.utc)  # afternoon in America/Chicago


class FakeGhlClient:
    """No live network: an in-memory stand-in for GoHighLevelWriteClient's
    note surface (create_note/list_notes/get_note)."""

    def __init__(self, fail_create_for: set[str] | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.get_note_calls = 0
        self.list_notes_calls = 0
        self._fail_create_for = fail_create_for or set()

    def list_notes(self, contact_id: str) -> list[dict]:
        self.list_notes_calls += 1
        return list(self.notes.get(contact_id, []))

    def create_note(self, contact_id: str, body: str, *, trigger: str) -> dict:
        assert trigger, "create_note must always receive a non-empty trigger"
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {"id": f"note-{self.create_calls}", "body": body, "contactId": contact_id}
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def get_note(self, contact_id: str, note_id: str) -> dict | None:
        self.get_note_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


@pytest.fixture()
def state_db(tmp_path: Path) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db")


# --- Note body privacy -------------------------------------------------------


class TestNoteBodyPrivacy:
    def test_body_contains_only_decision_useful_fields_and_the_marker(self) -> None:
        marker = note_marker_for_event("evt-abc123")
        body = format_note_body(
            occurred_at=OCCURRED_AT, direction="inbound", answered=1,
            duration_s=132, marker=marker,
        )
        lines = body.splitlines()
        assert len(lines) == 5
        assert lines[0].startswith("Desk call - ")
        assert lines[1] == "Direction: Incoming"
        assert lines[2] == "Status: Answered"
        assert lines[3] == "Duration: 2m 12s (132s)"
        assert lines[4] == marker

    def test_body_uses_america_chicago_display_time_with_timezone(self) -> None:
        body = format_note_body(
            occurred_at=OCCURRED_AT, direction="outbound", answered=0,
            duration_s=None, marker="[m]",
        )
        # 2026-09-21T20:14:00Z is 2026-09-21 03:14 PM in America/Chicago (CDT).
        assert "2026-09-21 03:14 PM CDT" in body
        assert "Direction: Outgoing" in body
        assert "Status: Missed" in body
        assert "Duration: unknown" in body

    def test_unknown_direction_and_answered_are_labeled_not_guessed(self) -> None:
        body = format_note_body(
            occurred_at=OCCURRED_AT, direction=None, answered=None,
            duration_s=0, marker="[m]",
        )
        assert "Direction: Unknown direction" in body
        assert "Status: Unknown status" in body
        assert "Duration: 0s" in body

    def test_marker_is_deterministic_and_non_reversible(self) -> None:
        a = note_marker_for_event("evt-1")
        b = note_marker_for_event("evt-1")
        c = note_marker_for_event("evt-2")
        assert a == b
        assert a != c
        assert "evt-1" in a  # embeds the ledger event_id verbatim, not a raw handle

    def test_contact_detail_url_has_no_note_specific_deep_link(self) -> None:
        url = contact_detail_url("loc-1", "c-1")
        assert "loc-1" in url and "c-1" in url
        assert "note" not in url.lower()


# --- Create / read-back contract --------------------------------------------


class TestMirrorEventCreatesAndVerifies:
    def test_first_call_creates_exactly_one_note_and_records_complete(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert result.outcome == "created"
        assert ghl.create_calls == 1
        assert ghl.get_note_calls == 1  # exact read-back before marking complete
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == result.note_id
        assert row.contact_id == "c-1"

    def test_a_read_back_missing_the_marker_does_not_mark_complete(
        self, state_db: IngestionStateDb, monkeypatch
    ) -> None:
        ghl = FakeGhlClient()
        monkeypatch.setattr(ghl, "get_note", lambda contact_id, note_id: {"id": note_id, "body": "wrong body"})
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert result.outcome == "error"
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "pending"


# --- No duplicates on retry ---------------------------------------------------


class TestNoDuplicateOnRetry:
    def test_retrying_the_same_event_id_never_posts_twice(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        first = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        second = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert second.outcome == "already_complete"
        assert second.note_id == first.note_id
        assert ghl.create_calls == 1


# --- Write-failure and local-state-failure recovery --------------------------


class TestWriteFailure:
    def test_create_note_failure_is_reported_as_error_and_leaves_state_pending(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient(fail_create_for={"c-1"})
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert result.outcome == "error"
        assert ghl.create_calls == 0
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "pending"

    def test_retry_after_the_destination_recovers_creates_the_note(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient(fail_create_for={"c-1"})
        mirror = NoteMirror(ghl, state_db)
        failed = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert failed.outcome == "error"

        ghl._fail_create_for.clear()
        recovered = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert recovered.outcome == "created"
        assert ghl.create_calls == 1


class TestLocalStateFailureRecovery:
    def test_a_lost_local_state_db_recovers_the_already_created_note_by_marker(
        self, tmp_path: Path
    ) -> None:
        """Simulates: create_note POSTed successfully to GHL, but the local
        state write (mark_note_mirror_complete) never landed -- e.g. a crash
        between the two. The next run, against a *fresh* state_db (as if the
        prior local write never happened), must discover the existing note
        via its marker and mark it complete without a second POST."""
        ghl = FakeGhlClient()
        state_db_a = IngestionStateDb(tmp_path / "state_a.db")
        mirror_a = NoteMirror(ghl, state_db_a)
        first = mirror_a.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert first.outcome == "created"
        assert ghl.create_calls == 1

        state_db_b = IngestionStateDb(tmp_path / "state_b.db")  # local state "lost"
        mirror_b = NoteMirror(ghl, state_db_b)
        recovered = mirror_b.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert recovered.outcome == "recovered"
        assert recovered.note_id == first.note_id
        assert ghl.create_calls == 1, "must not create a second note"
        row = state_db_b.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == first.note_id


# --- IngestionStateDb.note_mirror table --------------------------------------


class TestNoteMirrorStateDb:
    def test_attempt_then_complete_then_attempt_again_does_not_regress(
        self, state_db: IngestionStateDb
    ) -> None:
        assert state_db.get_note_mirror("evt-1") is None
        state_db.record_note_mirror_attempt("evt-1", contact_id="c-1", content_hash="h1")
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "pending"
        assert row.attempts == 1

        state_db.mark_note_mirror_complete(
            "evt-1", contact_id="c-1", note_id="note-1", content_hash="h1"
        )
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == "note-1"

        state_db.record_note_mirror_attempt("evt-1", contact_id="c-1", content_hash="h2")
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "complete", "an attempt after completion must not regress status"
        assert row.note_id == "note-1"
