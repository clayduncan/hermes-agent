"""Tests for the OPS-75 iMessage lane of the GHL note mirror
(plugins/team_duncan_contacts/note_mirror.py::mirror_message_event).

Uses the same fake GHL client / real IngestionStateDb pattern as
test_note_mirror.py (the OPS-18 call lane's own test file, left unmodified
by this build). Covers: exact body format with the visible marker, title/
color by direction, local-state fast path, the new pre-POST marker-search
recovery path, and that mirror_event's own behavior is unaffected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import (
    IMESSAGE_RECEIVED_COLOR,
    IMESSAGE_RECEIVED_TITLE,
    IMESSAGE_SENT_COLOR,
    IMESSAGE_SENT_TITLE,
    OK_OUTCOMES,
    NoteMirror,
    format_imessage_note_body,
    imessage_note_marker,
)

OCCURRED_AT = datetime(2026, 9, 22, 14, 3, tzinfo=timezone.utc)


class FakeGhlClient:
    """Same shape as test_note_mirror.py's FakeGhlClient, plus list_notes."""

    def __init__(self, fail_create_for: set[str] | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.get_note_calls = 0
        self.list_notes_calls = 0
        self.create_call_kwargs: list[dict] = []
        self._fail_create_for = fail_create_for or set()

    def create_note(
        self, contact_id: str, body: str, *, trigger: str, color: str | None = None,
        pinned: bool = False, title: str | None = None,
        redact_body_in_audit: bool = False,
    ) -> dict:
        assert trigger, "create_note must always receive a non-empty trigger"
        self.create_call_kwargs.append(
            {"color": color, "pinned": pinned, "title": title,
             "redact_body_in_audit": redact_body_in_audit}
        )
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {
            "id": f"note-{self.create_calls}", "body": body, "contactId": contact_id,
            "color": color, "pinned": pinned, "title": title,
        }
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def get_note(self, contact_id: str, note_id: str) -> dict | None:
        self.get_note_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None

    def list_notes(self, contact_id: str) -> list[dict]:
        self.list_notes_calls += 1
        return list(self.notes.get(contact_id, []))


@pytest.fixture()
def state_db(tmp_path: Path) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db")


# --- Marker -------------------------------------------------------------------


def test_marker_is_deterministic_and_derived_from_event_id() -> None:
    assert imessage_note_marker("abc123") == imessage_note_marker("abc123")


def test_marker_form_is_prefixed_and_truncated() -> None:
    eid = "0123456789abcdef" + "f" * 48  # 64-char sha256 hex
    marker = imessage_note_marker(eid)
    assert marker == "imsg-evt:0123456789abcdef"


def test_marker_differs_by_event_id() -> None:
    assert imessage_note_marker("evt-a") != imessage_note_marker("evt-b")


# --- Body format ----------------------------------------------------------------


def test_body_format_is_text_blank_line_timestamp_marker() -> None:
    marker = imessage_note_marker("evt-1")
    body = format_imessage_note_body(text="Hey, see you at 6", occurred_at=OCCURRED_AT, marker=marker)
    assert body == f"Hey, see you at 6\n\n2026-09-22 14:03 UTC · {marker}"


def test_body_text_is_verbatim_only_stripped() -> None:
    marker = imessage_note_marker("evt-1")
    body = format_imessage_note_body(text="  spaced out  \n", occurred_at=OCCURRED_AT, marker=marker)
    assert body.startswith("spaced out\n\n")


def test_body_converts_non_utc_occurred_at_to_utc() -> None:
    from datetime import timedelta

    tz_minus5 = timezone(timedelta(hours=-5))
    local_time = OCCURRED_AT.astimezone(tz_minus5)
    marker = imessage_note_marker("evt-1")
    body = format_imessage_note_body(text="hi", occurred_at=local_time, marker=marker)
    assert "2026-09-22 14:03 UTC" in body


# --- Title / color by direction --------------------------------------------------


class TestTitleAndColorByDirection:
    def test_sent_uses_sent_title_and_color(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        note = ghl.notes["c-1"][0]
        assert note["title"] == IMESSAGE_SENT_TITLE == "iMessage · Sent"
        assert note["color"] == IMESSAGE_SENT_COLOR == "#007AFF"

    def test_received_uses_received_title_and_color(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=False, trigger="t",
        )
        note = ghl.notes["c-1"][0]
        assert note["title"] == IMESSAGE_RECEIVED_TITLE == "iMessage · Received"
        assert note["color"] == IMESSAGE_RECEIVED_COLOR == "#8E8E93"


# --- Create / verify contract, local-state fast path -----------------------------


class TestMirrorMessageEventCreates:
    def test_first_call_creates_exactly_one_note_and_records_complete(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "created"
        assert ghl.create_calls == 1
        assert ghl.get_note_calls == 1
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == result.note_id

    def test_retry_the_same_event_id_uses_local_state_fast_path(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        first = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        ghl.list_notes_calls = 0
        second = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert second.outcome == "already_complete"
        assert second.note_id == first.note_id
        assert ghl.create_calls == 1
        assert ghl.list_notes_calls == 0  # never falls through to the marker search


# --- Pre-POST marker-search recovery ----------------------------------------------


class TestMarkerSearchRecovery:
    def test_lost_local_state_is_recovered_by_scanning_for_the_marker(
        self, tmp_path: Path
    ) -> None:
        ghl = FakeGhlClient()
        state_db_a = IngestionStateDb(tmp_path / "state_a.db")
        mirror_a = NoteMirror(ghl, state_db_a)
        first = mirror_a.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert first.outcome == "created"
        assert ghl.create_calls == 1

        state_db_b = IngestionStateDb(tmp_path / "state_b.db")  # local state "lost"
        mirror_b = NoteMirror(ghl, state_db_b)
        recovered = mirror_b.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert recovered.outcome == "recovered"
        assert recovered.note_id == first.note_id
        assert ghl.create_calls == 1, "must recover via marker search, never re-create"
        row = state_db_b.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == first.note_id

    def test_recovered_is_in_ok_outcomes(self) -> None:
        assert "recovered" in OK_OUTCOMES

    def test_no_marker_match_falls_through_to_create(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        ghl.notes["c-1"] = [{"id": "unrelated-note", "body": "some other note body"}]
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "created"
        assert ghl.list_notes_calls == 1

    def test_marker_matched_note_no_longer_resolving_is_an_error(
        self, state_db: IngestionStateDb, monkeypatch
    ) -> None:
        ghl = FakeGhlClient()
        marker = imessage_note_marker("evt-1")
        ghl.notes["c-1"] = [{"id": "note-x", "body": f"hi\n\n2026-09-22 14:03 UTC · {marker}"}]
        monkeypatch.setattr(ghl, "get_note", lambda contact_id, note_id: None)
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "error"
        assert ghl.create_calls == 0, "must not guess and silently re-create"

    def test_marker_search_failure_is_an_error_not_a_silent_create(
        self, state_db: IngestionStateDb, monkeypatch
    ) -> None:
        ghl = FakeGhlClient()
        monkeypatch.setattr(
            ghl, "list_notes", lambda contact_id: (_ for _ in ()).throw(RuntimeError("outage"))
        )
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "error"
        assert ghl.create_calls == 0


# --- Privacy: iMessage lane opts into audit redaction, call lane doesn't ---------


def test_mirror_message_event_opts_into_audit_redaction(state_db: IngestionStateDb) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert ghl.create_call_kwargs[0]["redact_body_in_audit"] is True


def test_call_lane_create_note_call_never_opts_into_audit_redaction(
    state_db: IngestionStateDb,
) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    mirror.mirror_event(
        event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
        direction="inbound", answered=1, duration_s=60, trigger="t",
    )
    assert ghl.create_call_kwargs[0]["redact_body_in_audit"] is False


# --- Dedupe key is event_id, and two different events with identical text --------


def test_dedupe_key_is_event_id_not_content(state_db: IngestionStateDb) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    first = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    second = mirror.mirror_message_event(
        event_id="evt-2", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert second.outcome == "created"
    assert second.note_id != first.note_id
    assert ghl.create_calls == 2


# --- Write failure -----------------------------------------------------------------


def test_create_note_failure_is_reported_as_error_and_leaves_state_pending(
    state_db: IngestionStateDb,
) -> None:
    ghl = FakeGhlClient(fail_create_for={"c-1"})
    mirror = NoteMirror(ghl, state_db)
    result = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert result.outcome == "error"
    row = state_db.get_note_mirror("evt-1")
    assert row.status == "pending"


# --- The call lane's own behavior is unaffected by this build --------------------


def test_call_lane_create_note_call_still_never_passes_title(
    state_db: IngestionStateDb,
) -> None:
    """mirror_event (the OPS-18 call lane) must never pass a title kwarg to
    create_note -- the shared _create_and_verify helper must only include
    title in the call when one is actually given."""
    calls: list[dict] = []

    class _RecordingGhl:
        def create_note(self, contact_id, body, **kwargs):
            calls.append(kwargs)
            return {"id": "note-1", "body": body}

        def get_note(self, contact_id, note_id):
            return {"id": note_id, "body": "Incoming call · Answered · 1 min"}

    mirror = NoteMirror(_RecordingGhl(), state_db)
    mirror.mirror_event(
        event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
        direction="inbound", answered=1, duration_s=60, trigger="t",
    )
    assert "title" not in calls[0]
