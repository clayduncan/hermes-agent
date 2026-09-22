"""Tests for the OPS-110 Plaud-summary GHL note writer.

Uses a fake GHL client (no live network) and the real IngestionStateDb
against a temp sqlite file -- the same note_mirror table note_mirror.py's
NoteMirror uses, proving the two pipelines share one dedupe identity per
Desk event.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import CALL_NOTE_COLOR, NoteMirror
from plugins.team_duncan_contacts.plaud_note_writer import (
    PlaudSummaryNoteWriter,
    desk_event_id,
)


class FakeGhlClient:
    def __init__(self, fail_create_for: set[str] | None = None, fail_update_for: set[str] | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.update_calls = 0
        self.get_note_calls = 0
        self._fail_create_for = fail_create_for or set()
        self._fail_update_for = fail_update_for or set()

    def create_note(self, contact_id, body, *, trigger, color=None, pinned=False, title=None):
        assert trigger, "create_note must always receive a non-empty trigger"
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {
            "id": f"note-{self.create_calls}", "body": body, "contactId": contact_id,
            "color": color, "pinned": pinned, "title": title,
        }
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def update_note(self, contact_id, note_id, body, *, trigger, color=None, pinned=None, userId=None, title=None):
        assert trigger, "update_note must always receive a non-empty trigger"
        if contact_id in self._fail_update_for:
            raise RuntimeError("simulated GHL outage")
        self.update_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                n["body"] = body
                n["color"] = color
                n["title"] = title
                return dict(n)
        raise AssertionError("update_note called for a note that doesn't exist")

    def get_note(self, contact_id, note_id):
        self.get_note_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


@pytest.fixture()
def state_db(tmp_path: Path) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db")


TITLE = "Incoming call · Answered · 21 min 46 sec"
SUMMARY_LINES = [
    "Clay and Cory discussed a new listing referral.",
    "Cory will send the seller's contact information this week.",
]


class TestCreatesWhenNoExistingNote:
    def test_first_write_creates_a_note(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        result = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        assert result.outcome == "created"
        assert ghl.create_calls == 1
        note = ghl.notes["c-1"][0]
        assert note["body"] == "\n".join(SUMMARY_LINES)
        assert note["title"] == TITLE
        assert note["color"] == CALL_NOTE_COLOR

    def test_body_is_exactly_the_summary_lines_joined_by_newline(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=["one.", "two.", "three."], trigger="t",
        )
        assert ghl.notes["c-1"][0]["body"] == "one.\ntwo.\nthree."


class TestUpdatesExistingDeskNoteInPlace:
    def test_a_note_already_mirrored_by_desk_only_ingestion_is_updated_not_duplicated(
        self, state_db: IngestionStateDb
    ) -> None:
        """Simulates the ticket's real scenario: note `4oeElgwlgtrGwgIyvCqw`
        already exists via the Desk-only NoteMirror for this Desk event.
        The Plaud summary writer must find that exact note (same event_id
        identity) and update it, never create a second one."""
        ghl = FakeGhlClient()
        desk_mirror = NoteMirror(ghl, state_db)
        desk_mirror.mirror_event(
            event_id=desk_event_id("desk-1"), contact_id="c-1",
            occurred_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
            direction="inbound", answered=1, duration_s=1306, trigger="desk-mirror",
        )
        assert ghl.create_calls == 1
        existing_note_id = ghl.notes["c-1"][0]["id"]

        writer = PlaudSummaryNoteWriter(ghl, state_db)
        result = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="plaud-summary",
        )
        assert result.outcome == "updated"
        assert result.note_id == existing_note_id
        assert ghl.create_calls == 1, "must not create a second note"
        assert ghl.update_calls == 1
        assert ghl.notes["c-1"][0]["body"] == "\n".join(SUMMARY_LINES)


class TestIdempotentReplay:
    def test_writing_the_identical_summary_twice_does_not_duplicate_or_reupdate(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        first = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        second = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        assert first.outcome == "created"
        assert second.outcome == "already_current"
        assert second.note_id == first.note_id
        assert ghl.create_calls == 1
        assert ghl.update_calls == 0

    def test_already_current_verifies_by_reading_the_note_back(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        ghl.get_note_calls = 0
        result = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        assert result.outcome == "already_current"
        assert ghl.get_note_calls == 1

    def test_a_different_summary_for_the_same_desk_event_updates_in_place(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        first = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        second = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=["A revised summary line.", "Another revised line."], trigger="t",
        )
        assert second.outcome == "updated"
        assert second.note_id == first.note_id
        assert ghl.create_calls == 1
        assert ghl.update_calls == 1
        assert ghl.notes["c-1"][0]["body"] == "A revised summary line.\nAnother revised line."


class TestDedupeKeyIsDeskEventNotPlaudRecording:
    def test_two_different_desk_events_each_get_their_own_note(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        first = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        second = writer.write(
            desk_source_event_id="desk-2", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        assert second.outcome == "created"
        assert second.note_id != first.note_id
        assert ghl.create_calls == 2


class TestWriteFailure:
    def test_create_failure_is_reported_as_error(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient(fail_create_for={"c-1"})
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        result = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        assert result.outcome == "error"
        assert ghl.create_calls == 0

    def test_update_failure_is_reported_as_error_and_old_note_row_is_untouched(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        writer = PlaudSummaryNoteWriter(ghl, state_db)
        first = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=SUMMARY_LINES, trigger="t",
        )
        ghl._fail_update_for.add("c-1")
        result = writer.write(
            desk_source_event_id="desk-1", contact_id="c-1", title=TITLE,
            summary_lines=["A different summary line.", "Second line."], trigger="t",
        )
        assert result.outcome == "error"
        row = state_db.get_note_mirror(desk_event_id("desk-1"))
        assert row.note_id == first.note_id
        assert row.status == "complete"
