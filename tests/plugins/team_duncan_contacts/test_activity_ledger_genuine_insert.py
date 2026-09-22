"""Tests for RecordResult.genuine_insert (OPS-75 §4).

Reuses test_activity_ledger.py's own fake registry pattern. genuine_insert
distinguishes "this call just created a new ledger row" from "this call
replayed an event admitted on a prior run" -- record_event()'s outcome
alone cannot make that distinction, which is exactly why the iMessage
collector needs this field to gate note-mirroring against a rolling
extraction window without re-mirroring or backfill-mirroring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import TEAM_DUNCAN_LOCATION_ID, ActivityLedger

_LOC = TEAM_DUNCAN_LOCATION_ID
_CONTACT_A = "contactAAA"
_HANDLE_A = "handle-alpha"
_T_AFTER = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
_T_BEFORE = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)


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

    def set_deny_pre_activation(self, handle: str, contact_id: str, location_id: str = _LOC) -> None:
        self._responses[handle] = ("deny_pre_activation", contact_id, location_id)

    def resolve_event(self, raw_handle: str, event_ts: datetime) -> _FakeResolveResult:
        if raw_handle not in self._responses:
            return _FakeResolveResult("review_required")
        decision, cid, lid = self._responses[raw_handle]
        return _FakeResolveResult(decision, cid, lid)


@pytest.fixture()
def tmp_ledger(tmp_path: Path):
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    return ledger, reg


def test_first_admission_is_genuine_insert(tmp_ledger):
    ledger, _ = tmp_ledger
    result = ledger.record_event("imessage", "guid-1", _HANDLE_A, _T_AFTER, {})
    assert result.outcome == "admitted"
    assert result.genuine_insert is True


def test_replayed_admission_is_not_genuine_insert(tmp_ledger):
    ledger, _ = tmp_ledger
    first = ledger.record_event("imessage", "guid-1", _HANDLE_A, _T_AFTER, {})
    second = ledger.record_event("imessage", "guid-1", _HANDLE_A, _T_AFTER, {})
    assert first.genuine_insert is True
    assert second.outcome == "admitted"
    assert second.genuine_insert is False
    assert second.event_id == first.event_id


def test_first_override_admission_is_genuine_insert(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "imessage", "guid-pre", "approved by clay")
    result = ledger.record_event("imessage", "guid-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "override_admitted"
    assert result.genuine_insert is True


def test_replayed_override_admission_is_not_genuine_insert(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "imessage", "guid-pre", "approved by clay")
    first = ledger.record_event("imessage", "guid-pre", _HANDLE_A, _T_BEFORE, {})
    second = ledger.record_event("imessage", "guid-pre", _HANDLE_A, _T_BEFORE, {})
    assert first.genuine_insert is True
    assert second.genuine_insert is False


def test_discarded_is_never_genuine_insert(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)  # no grant issued
    result = ledger.record_event("imessage", "guid-x", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert result.genuine_insert is False


def test_discarded_review_required_is_never_genuine_insert(tmp_ledger):
    ledger, _ = tmp_ledger
    result = ledger.record_event("imessage", "guid-y", "unknown-handle", _T_AFTER, {})
    assert result.outcome == "discarded"
    assert result.genuine_insert is False


def test_overlapping_window_genuine_insert_only_once_across_three_replays(tmp_ledger):
    """Simulates a rolling extraction window re-scanning the same event on
    three separate collector runs: only the very first record_event() call
    for that (source, source_event_id) is a genuine insert."""
    ledger, _ = tmp_ledger
    results = [
        ledger.record_event("imessage", "guid-1", _HANDLE_A, _T_AFTER, {})
        for _ in range(3)
    ]
    assert [r.genuine_insert for r in results] == [True, False, False]
