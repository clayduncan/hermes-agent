"""Tests for the OPS-18 GHL note mirror (plugins/team_duncan_contacts/note_mirror.py).

Uses a fake GHL client (no live network) and the real IngestionStateDb
against a temp sqlite file. Covers: duration formatting, the exact clean
one-line body (no marker/date/labels), local-state-only dedupe, and the
create/read-back contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import (
    CALL_NOTE_COLOR,
    NoteMirror,
    contact_detail_url,
    format_note_body,
)

OCCURRED_AT = datetime(2026, 9, 21, 20, 14, tzinfo=timezone.utc)  # afternoon in America/Chicago


class FakeGhlClient:
    """No live network: an in-memory stand-in for GoHighLevelWriteClient's
    note surface (create_note/get_note)."""

    def __init__(self, fail_create_for: set[str] | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.get_note_calls = 0
        self._fail_create_for = fail_create_for or set()

    def create_note(
        self, contact_id: str, body: str, *, trigger: str, color: str | None = None,
        pinned: bool = False,
    ) -> dict:
        assert trigger, "create_note must always receive a non-empty trigger"
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {
            "id": f"note-{self.create_calls}", "body": body, "contactId": contact_id,
            "color": color, "pinned": pinned,
        }
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


# --- Duration formatting ------------------------------------------------------


class TestDurationFormatting:
    @pytest.mark.parametrize(
        "duration_s,expected",
        [
            (0, "0 sec"),
            (1, "1 sec"),
            (5, "5 sec"),
            (59, "59 sec"),
            (60, "1 min"),
            (120, "2 min"),
            (1306, "21 min 46 sec"),
            (61, "1 min 1 sec"),
            (65, "1 min 5 sec"),
        ],
    )
    def test_duration_forms(self, duration_s: int, expected: str) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=duration_s)
        assert body == f"Incoming call · Answered · {expected}"

    def test_negative_duration_normalizes_to_zero(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=-5)
        assert body.endswith("0 sec")

    def test_float_duration_truncates_to_whole_seconds(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=65.9)
        assert body.endswith("1 min 5 sec")


# --- Exact clean strings, ground-truth examples -------------------------------


class TestExactCleanStrings:
    def test_incoming_answered_example(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=1306)
        assert body == "Incoming call · Answered · 21 min 46 sec"

    def test_outgoing_missed_example(self) -> None:
        body = format_note_body(direction="outbound", answered=0, duration_s=5)
        assert body == "Outgoing call · Missed · 5 sec"

    def test_body_is_exactly_one_line(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=132)
        assert "\n" not in body
        assert len(body.splitlines()) == 1


# --- Absence of marker / date / labels ----------------------------------------


class TestNoHiddenOrLabeledContent:
    def test_no_event_marker_in_the_body(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=60)
        assert "ops18-event" not in body
        assert "[" not in body and "]" not in body

    def test_no_date_in_the_body(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=60)
        assert "2026" not in body
        assert "AM" not in body and "PM" not in body

    def test_no_field_labels_in_the_body(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=60)
        assert "Direction:" not in body
        assert "Status:" not in body
        assert "Duration:" not in body
        assert "Desk call" not in body

    def test_no_zero_width_or_html_comment_characters(self) -> None:
        body = format_note_body(direction="inbound", answered=1, duration_s=60)
        assert "​" not in body  # zero-width space
        assert "‌" not in body  # zero-width non-joiner
        assert "<!--" not in body

    def test_format_note_body_takes_no_occurred_at_or_marker_argument(self) -> None:
        import inspect

        params = set(inspect.signature(format_note_body).parameters)
        assert params == {"direction", "answered", "duration_s"}

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

    def test_created_note_body_is_the_exact_clean_line(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="outbound", answered=0, duration_s=5, trigger="t",
        )
        written = ghl.notes["c-1"][0]["body"]
        assert written == "Outgoing call · Missed · 5 sec"

    def test_a_read_back_with_a_different_body_does_not_mark_complete(
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


# --- Green call-note color -----------------------------------------------------


class TestCallNoteColor:
    def test_call_note_color_is_the_fixed_light_green(self) -> None:
        assert CALL_NOTE_COLOR == "#D9EAD3"

    def test_create_note_always_receives_the_fixed_call_note_color(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert ghl.notes["c-1"][0]["color"] == CALL_NOTE_COLOR

    def test_every_future_mirrored_call_gets_the_green_color_not_just_the_first(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        mirror.mirror_event(
            event_id="evt-2", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="outbound", answered=0, duration_s=5, trigger="t",
        )
        colors = [note["color"] for note in ghl.notes["c-1"]]
        assert colors == [CALL_NOTE_COLOR, CALL_NOTE_COLOR]


# --- Local-state dedupe (no marker, no GHL body scan) -------------------------


class TestLocalStateDedupe:
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

    def test_already_complete_verifies_by_reading_the_note_back_not_by_scanning_bodies(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        ghl.get_note_calls = 0
        result = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert result.outcome == "already_complete"
        assert ghl.get_note_calls == 1  # verified via GET, no list_notes/body scan
        assert not hasattr(ghl, "list_notes")

    def test_already_complete_but_note_no_longer_resolves_is_an_error_not_a_guess(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        first = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        ghl.notes["c-1"] = []  # the note is gone from GHL's side
        result = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert result.outcome == "error"
        assert ghl.create_calls == 1, "must not guess and silently re-create"
        assert result.note_id == first.note_id

    def test_dedupe_key_is_event_id_not_content(self, state_db: IngestionStateDb) -> None:
        """Two different events with identical direction/answered/duration
        (and thus identical bodies) must each get their own note -- dedupe
        is keyed on event_id, never on note content."""
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        first = mirror.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        second = mirror.mirror_event(
            event_id="evt-2", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert second.outcome == "created"
        assert second.note_id != first.note_id
        assert ghl.create_calls == 2


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


class TestLocalStateLossIsAnUnavoidableLimitation:
    """No marker is ever written into a note body, so a lost local
    note_mirror row (with no backup) cannot be recovered by searching GHL:
    the next attempt creates a second note. This is documented as an
    unavoidable limitation in note_mirror.py, not a bug -- this test
    proves the (undesirable but expected) behavior rather than a false
    guarantee of recovery."""

    def test_a_lost_local_state_db_creates_a_second_note_it_cannot_avoid(
        self, tmp_path: Path
    ) -> None:
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
        again = mirror_b.mirror_event(
            event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
            direction="inbound", answered=1, duration_s=60, trigger="t",
        )
        assert again.outcome == "created"
        assert ghl.create_calls == 2, "no marker to recover by -- a second note is unavoidable"
        assert again.note_id != first.note_id


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
