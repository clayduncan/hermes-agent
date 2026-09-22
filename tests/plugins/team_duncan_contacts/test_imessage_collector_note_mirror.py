"""Tests for the OPS-75 note-mirror wiring in the iMessage collector
(plugins/team_duncan_contacts/collectors/imessage_collector.py §5).

Reuses test_imessage_collector.py's own fake registry / temp-ledger
pattern (that file itself is left unmodified by this build -- its own
`text not in provenance` assertion still holds). No live network: a
FakeNoteMirror records every call it receives instead of touching GHL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import TEAM_DUNCAN_LOCATION_ID, ActivityLedger
from plugins.team_duncan_contacts.collectors import imessage_collector
from plugins.team_duncan_contacts.collectors.imessage_collector import collect
from plugins.team_duncan_contacts.note_mirror import MirrorResult

_LOC = TEAM_DUNCAN_LOCATION_ID
_CONTACT_A = "contactAAA"
_CONTACT_B = "contactBBB"
_HANDLE_A = "+15550001111"
_HANDLE_B = "+15550002222"

_WINDOW_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
_EVENT_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResolveResult:
    def __init__(self, decision: str, contact_id: str | None = None, location_id: str | None = None) -> None:
        self.decision = decision
        self.ghl_contact_id = contact_id
        self.location_id = location_id


class _FakeRegistry:
    def __init__(self) -> None:
        self._responses: dict[str, tuple[str, str | None, str | None]] = {}

    def set_allow(self, handle: str, contact_id: str, location_id: str = _LOC) -> None:
        self._responses[handle] = ("allow", contact_id, location_id)

    def resolve_event(self, raw_handle: str, event_ts: datetime) -> _FakeResolveResult:
        if raw_handle not in self._responses:
            return _FakeResolveResult("review_required")
        decision, cid, lid = self._responses[raw_handle]
        return _FakeResolveResult(decision, cid, lid)


@pytest.fixture()
def tmp_ledger(tmp_path: Path):
    reg = _FakeRegistry()
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    return ledger, reg


class _FakeNoteMirror:
    def __init__(self, outcome: str = "created") -> None:
        self.calls: list[dict] = []
        self._outcome = outcome
        self._n = 0

    def mirror_message_event(self, **kwargs) -> MirrorResult:
        self.calls.append(kwargs)
        self._n += 1
        note_id = f"note-{self._n}" if self._outcome in ("created", "recovered") else None
        return MirrorResult(outcome=self._outcome, note_id=note_id)


def _make_event(
    *,
    source_event_id: str = "guid-1",
    occurred_at: datetime = _EVENT_TS,
    is_from_me: bool = False,
    is_group_chat: bool = False,
    chat_guid: str | None = "chat-1",
    handle_raw: str | None = _HANDLE_A,
    participant_handles: list[str] | None = None,
    text: str | None = "hello there",
) -> dict:
    event = {
        "source_event_id": source_event_id,
        "occurred_at": occurred_at,
        "is_from_me": is_from_me,
        "is_group_chat": is_group_chat,
        "chat_guid": chat_guid,
        "handle_raw": handle_raw,
        "attachment_kinds": [],
        "has_attachments": False,
        "text": text,
    }
    if participant_handles is not None:
        event["participant_handles"] = participant_handles
    return event


class _RunnerSpy:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def __call__(self, window_start: datetime, window_end: datetime) -> list[dict]:
        return self._events


# --- genuine_insert gating -----------------------------------------------------


def test_genuinely_new_admission_is_mirrored(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror()
    runner = _RunnerSpy([_make_event(text="hi there")])

    summary = collect(
        reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END,
        extract_runner=runner, note_mirror=mirror,
    )

    assert len(mirror.calls) == 1
    assert mirror.calls[0]["text"] == "hi there"
    assert mirror.calls[0]["contact_id"] == _CONTACT_A
    assert mirror.calls[0]["is_from_me"] is False
    assert summary.notes_created == 1
    assert summary.note_references == [
        {
            "note_id": "note-1",
            "contact_id": _CONTACT_A,
            "contact_url": imessage_collector.contact_detail_url(_LOC, _CONTACT_A),
        }
    ]


def test_overlapping_window_replay_is_never_re_mirrored(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror()
    runner = _RunnerSpy([_make_event()])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner, note_mirror=mirror)
    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner, note_mirror=mirror)

    assert len(mirror.calls) == 1, "a replayed (non-genuine) admission must never be re-mirrored"


def test_no_note_mirror_supplied_is_a_no_op(tmp_ledger):
    """Every pre-existing caller/test (note_mirror=None, the default) is
    unaffected: admission proceeds and no mirror call is attempted."""
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    runner = _RunnerSpy([_make_event()])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.admitted == 1
    assert summary.notes_created == 0
    assert summary.note_references == []


def test_discarded_event_is_never_mirrored(tmp_ledger):
    ledger, reg = tmp_ledger  # no allow configured -> review_required -> discarded
    mirror = _FakeNoteMirror()
    runner = _RunnerSpy([_make_event(handle_raw="+15559998888")])

    summary = collect(
        reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END,
        extract_runner=runner, note_mirror=mirror,
    )

    assert summary.discarded == 1
    assert mirror.calls == []


# --- outcome counting -----------------------------------------------------------


def test_recovered_outcome_is_counted_separately_from_created(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror(outcome="recovered")
    runner = _RunnerSpy([_make_event()])

    summary = collect(
        reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END,
        extract_runner=runner, note_mirror=mirror,
    )

    assert summary.notes_recovered == 1
    assert summary.notes_created == 0
    assert summary.note_errors == 0


def test_error_outcome_is_counted_and_does_not_raise(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror(outcome="error")
    runner = _RunnerSpy([_make_event()])

    summary = collect(
        reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END,
        extract_runner=runner, note_mirror=mirror,
    )

    assert summary.note_errors == 1
    assert summary.notes_created == 0
    assert summary.notes_recovered == 0
    assert summary.note_references == []
    # The ledger admission itself is unaffected by a mirror failure.
    assert summary.admitted == 1
    assert ledger.query_events(_CONTACT_A)


# --- group chat: one note per participant contact --------------------------------


def test_group_chat_outbound_mirrors_once_per_admitted_participant(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    mirror = _FakeNoteMirror()
    runner = _RunnerSpy(
        [
            _make_event(
                source_event_id="guid-group-1",
                is_from_me=True,
                is_group_chat=True,
                chat_guid="chat-group-1",
                handle_raw=None,
                participant_handles=[_HANDLE_A, _HANDLE_B],
                text="see you all there",
            )
        ]
    )

    summary = collect(
        reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END,
        extract_runner=runner, note_mirror=mirror,
    )

    assert summary.notes_created == 2
    contact_ids = {c["contact_id"] for c in mirror.calls}
    assert contact_ids == {_CONTACT_A, _CONTACT_B}
    assert all(c["is_from_me"] is True for c in mirror.calls)
    assert all(c["text"] == "see you all there" for c in mirror.calls)


# --- text discipline: not stored in provenance, not required on the event --------


def test_missing_text_field_defaults_to_empty_string(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror()
    event = _make_event()
    del event["text"]
    runner = _RunnerSpy([event])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner, note_mirror=mirror)

    assert mirror.calls[0]["text"] == ""


def test_text_never_reaches_provenance(tmp_ledger):
    import json

    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    mirror = _FakeNoteMirror()
    runner = _RunnerSpy([_make_event(text="this is a secret message")])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner, note_mirror=mirror)

    rows = ledger.query_events(_CONTACT_A)
    assert "this is a secret message" not in rows[0].provenance_json
