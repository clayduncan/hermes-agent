"""Tests for the OPS-75 §5 bounded one-shot exact-source-event path and the
production note-mirror wiring in imessage_collector.py.

No live subprocess, no live GHL network: _default_extract_runner is
monkeypatched for the one-shot tests, and _build_note_mirror's wiring is
checked for object identity/scope only (same no-network-at-construction
pattern as test_ingestion_runner_factory_wiring.py).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import TEAM_DUNCAN_LOCATION_ID, ActivityLedger
from plugins.team_duncan_contacts.collectors import imessage_collector
from plugins.team_duncan_contacts.note_mirror import MirrorResult
from tools.ghl_client import TEAM_DUNCAN_ACCOUNT_KEY, GoHighLevelWriteClient

_LOC = TEAM_DUNCAN_LOCATION_ID
_CONTACT_A = "contactAAA"
_HANDLE_A = "+15550001111"
_AROUND = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


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


class _FakeNoteMirror:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def mirror_message_event(self, **kwargs) -> MirrorResult:
        self.calls.append(kwargs)
        return MirrorResult(outcome="created", note_id="note-live-1")


def _event(source_event_id: str = "guid-live-1") -> dict:
    return {
        "source_event_id": source_event_id,
        "occurred_at": _AROUND,
        "is_from_me": False,
        "is_group_chat": False,
        "chat_guid": "chat-1",
        "handle_raw": _HANDLE_A,
        "attachment_kinds": [],
        "has_attachments": False,
        "text": "hi",
    }


def _one_shot_args(*, source_event_id="guid-live-1", around_iso=_AROUND.isoformat(), window_minutes=60):
    return argparse.Namespace(
        source_event_id=source_event_id, around_iso=around_iso, window_minutes=window_minutes,
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    return ActivityLedger(tmp_path / "activity.db", reg), reg


# --- exactly one match: success ---------------------------------------------------


def test_exactly_one_match_creates_one_note_and_prints_reference(ledger, monkeypatch, capsys):
    led, reg = ledger
    monkeypatch.setattr(imessage_collector, "_default_extract_runner", lambda s, e: [_event()])
    mirror = _FakeNoteMirror()

    rc = imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args())

    assert rc == 0
    assert len(mirror.calls) == 1
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["note_id"] == "note-live-1"
    assert _LOC in printed["contact_url"]
    assert _CONTACT_A in printed["contact_url"]
    rows = led.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].source_event_id == "guid-live-1"


def test_one_shot_creates_at_most_one_note_even_if_called_twice(ledger, monkeypatch):
    led, reg = ledger
    monkeypatch.setattr(imessage_collector, "_default_extract_runner", lambda s, e: [_event()])
    mirror = _FakeNoteMirror()

    imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args())
    imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args())

    assert len(mirror.calls) == 1, "idempotent: genuine_insert gates the second run's mirror call"


# --- zero matches: hard failure, nothing created ----------------------------------


def test_zero_matches_is_an_error_and_creates_nothing(ledger, monkeypatch, capsys):
    led, reg = ledger
    monkeypatch.setattr(imessage_collector, "_default_extract_runner", lambda s, e: [])
    mirror = _FakeNoteMirror()

    rc = imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args())

    assert rc != 0
    assert mirror.calls == []
    assert led.query_events(_CONTACT_A) == []


# --- more than one match: hard integrity failure, refuses to guess ---------------


def test_multiple_matches_is_an_error_and_creates_nothing(ledger, monkeypatch):
    led, reg = ledger
    monkeypatch.setattr(
        imessage_collector, "_default_extract_runner",
        lambda s, e: [_event(), _event()],
    )
    mirror = _FakeNoteMirror()

    rc = imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args())

    assert rc != 0
    assert mirror.calls == []
    assert led.query_events(_CONTACT_A) == []


# --- missing --around-iso ----------------------------------------------------------


def test_missing_around_iso_is_rejected(ledger):
    led, reg = ledger
    mirror = _FakeNoteMirror()
    rc = imessage_collector._run_one_shot(reg, led, mirror, _one_shot_args(around_iso=None))
    assert rc != 0
    assert mirror.calls == []


# --- window-minutes is capped, never an open sweep ---------------------------------


def test_window_minutes_is_capped(ledger, monkeypatch):
    led, reg = ledger
    seen_windows = []

    def _spy_runner(window_start, window_end):
        seen_windows.append((window_start, window_end))
        return [_event()]

    monkeypatch.setattr(imessage_collector, "_default_extract_runner", _spy_runner)
    mirror = _FakeNoteMirror()

    imessage_collector._run_one_shot(
        reg, led, mirror, _one_shot_args(window_minutes=10 ** 9),
    )

    window_start, window_end = seen_windows[0]
    span_minutes = (window_end - window_start).total_seconds() / 60
    assert span_minutes <= imessage_collector._ONE_SHOT_MAX_WINDOW_MINUTES + 1


# --- no hard-coded identity in the source ------------------------------------------


def test_one_shot_path_hard_codes_no_phone_or_guid_literal():
    import inspect

    source = Path(inspect.getsourcefile(imessage_collector)).read_text(encoding="utf-8")
    one_shot_src = source[source.index("def _run_one_shot") : source.index("def main(")]
    assert "+1" not in one_shot_src  # no literal phone number
    assert "guid-" not in one_shot_src  # no literal message guid


# --- production note-mirror wiring (§5): object identity/scope only, no I/O -------


def test_build_note_mirror_wires_scoped_client_and_shared_state_db(tmp_path: Path) -> None:
    mirror = imessage_collector._build_note_mirror(tmp_path)

    assert isinstance(mirror._ghl, GoHighLevelWriteClient)
    assert mirror._ghl.account_key == TEAM_DUNCAN_ACCOUNT_KEY
    assert mirror._ghl.location_id == _LOC

    expected_db_path = tmp_path / "plugin-data" / "team_duncan_contacts" / "ingestion_state.db"
    assert mirror._state_db._db_path == expected_db_path
    assert expected_db_path.parent.exists()
