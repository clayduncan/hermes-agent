"""Tests for the iMessage CRM-lane collector (OPS-76, spec section 5).

Uses the same fixture-registry / temp-file-ActivityLedger pattern already
established by test_activity_ledger.py. No live SSH, no real chat.db, no
network: extract_runner is always a fixture function injected directly.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import TEAM_DUNCAN_LOCATION_ID, ActivityLedger
from plugins.team_duncan_contacts.collectors import imessage_collector
from plugins.team_duncan_contacts.collectors.imessage_collector import CollectSummary, collect

_LOC = TEAM_DUNCAN_LOCATION_ID
_CONTACT_A = "contactAAA"
_CONTACT_B = "contactBBB"
_CONTACT_C = "contactCCC"
_HANDLE_A = "+15550001111"
_HANDLE_B = "+15550002222"
_HANDLE_C = "+15550003333"

_WINDOW_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
_EVENT_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResolveResult:
    def __init__(self, decision: str, contact_id: str | None = None, location_id: str | None = None) -> None:
        self.decision = decision
        self.ghl_contact_id = contact_id
        self.location_id = location_id


class _FakeRegistry:
    """Configurable fake registry, mirroring test_activity_ledger.py's own fixture."""

    def __init__(self) -> None:
        self._responses: dict[str, tuple[str, str | None, str | None]] = {}
        self._calls: list[tuple[str, datetime]] = []

    def set_allow(self, handle: str, contact_id: str, location_id: str = _LOC) -> None:
        self._responses[handle] = ("allow", contact_id, location_id)

    def set_deny_pre_activation(self, handle: str, contact_id: str, location_id: str = _LOC) -> None:
        self._responses[handle] = ("deny_pre_activation", contact_id, location_id)

    def resolve_event(self, raw_handle: str, event_ts: datetime) -> _FakeResolveResult:
        self._calls.append((raw_handle, event_ts))
        if raw_handle not in self._responses:
            return _FakeResolveResult("review_required")
        decision, cid, lid = self._responses[raw_handle]
        return _FakeResolveResult(decision, cid, lid)

    @property
    def call_count(self) -> int:
        return len(self._calls)


@pytest.fixture()
def tmp_ledger(tmp_path: Path):
    reg = _FakeRegistry()
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    return ledger, reg


def _make_event(
    *,
    source_event_id: str = "guid-1",
    occurred_at: datetime = _EVENT_TS,
    is_from_me: bool = False,
    is_group_chat: bool = False,
    chat_guid: str | None = "chat-1",
    handle_raw: str | None = _HANDLE_A,
    participant_handles: list[str] | None = None,
    attachment_kinds: list[str] | None = None,
    has_attachments: bool = False,
) -> dict:
    event = {
        "source_event_id": source_event_id,
        "occurred_at": occurred_at,
        "is_from_me": is_from_me,
        "is_group_chat": is_group_chat,
        "chat_guid": chat_guid,
        "handle_raw": handle_raw,
        "attachment_kinds": attachment_kinds or [],
        "has_attachments": has_attachments,
    }
    if participant_handles is not None:
        event["participant_handles"] = participant_handles
    return event


class _RunnerSpy:
    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.call_count = 0

    def __call__(self, window_start: datetime, window_end: datetime) -> list[dict]:
        self.call_count += 1
        return self._events


# ---------------------------------------------------------------------------
# 1. allow-decision event admitted with correct provenance
# ---------------------------------------------------------------------------


def test_allow_event_admitted_with_provenance(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    runner = _RunnerSpy([_make_event(is_from_me=True, has_attachments=True, attachment_kinds=["image"])])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.admitted == 1
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    provenance = json.loads(rows[0].provenance_json)
    assert provenance["direction"] == "outbound"
    assert provenance["is_group_chat"] is False
    assert provenance["chat_guid"] == "chat-1"
    assert provenance["attachment_kinds"] == ["image"]
    assert "text" not in provenance


# ---------------------------------------------------------------------------
# 2. deny_pre_activation, no grant: discarded, zero rows
# ---------------------------------------------------------------------------


def test_deny_pre_activation_no_grant_discarded(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    runner = _RunnerSpy([_make_event()])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.discarded == 1
    assert summary.admitted == 0
    assert ledger.query_events(_CONTACT_A) == []


# ---------------------------------------------------------------------------
# 3. deny_pre_activation with a matching grant: override_admitted
# ---------------------------------------------------------------------------


def test_deny_pre_activation_with_grant_admitted(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "imessage", "guid-1", "approved by clay")
    runner = _RunnerSpy([_make_event()])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.override_admitted == 1
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].state == "override_admitted"


# ---------------------------------------------------------------------------
# 4. non-activated handle (review_required): zero rows
# ---------------------------------------------------------------------------


def test_review_required_zero_rows(tmp_ledger):
    ledger, reg = tmp_ledger
    runner = _RunnerSpy([_make_event(handle_raw="+15559998888")])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.discarded == 1
    assert summary.admitted == 0
    assert summary.override_admitted == 0


# ---------------------------------------------------------------------------
# 5. overlap idempotency
# ---------------------------------------------------------------------------


def test_overlapping_window_idempotent(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    runner = _RunnerSpy([_make_event()])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)
    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 6. group chat: one of three participants activated
# ---------------------------------------------------------------------------


def test_group_chat_one_of_three_activated(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    runner = _RunnerSpy(
        [
            _make_event(
                source_event_id="guid-group-1",
                is_from_me=True,
                is_group_chat=True,
                chat_guid="chat-group-1",
                handle_raw=None,
                participant_handles=[_HANDLE_A, _HANDLE_B, _HANDLE_C],
            )
        ]
    )

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.admitted == 1
    assert ledger.query_events(_CONTACT_A) == []
    assert ledger.query_events(_CONTACT_C) == []
    rows_b = ledger.query_events(_CONTACT_B)
    assert len(rows_b) == 1
    provenance = json.loads(rows_b[0].provenance_json)
    assert provenance["is_group_chat"] is True
    assert provenance["chat_guid"] == "chat-group-1"


# ---------------------------------------------------------------------------
# 7. raw handle unreachable outside resolve_event()/record_event()
# ---------------------------------------------------------------------------


def test_raw_handle_never_leaks(tmp_ledger):
    ledger, reg = tmp_ledger
    canary = "+15557778899"
    reg.set_allow(canary, _CONTACT_A)
    runner = _RunnerSpy([_make_event(handle_raw=canary, is_from_me=True)])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    export = ledger.export_ledger()
    assert canary not in json.dumps(export)


def test_group_chat_raw_handles_never_leak(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    runner = _RunnerSpy(
        [
            _make_event(
                source_event_id="guid-group-2",
                is_from_me=True,
                is_group_chat=True,
                chat_guid="chat-group-2",
                handle_raw=None,
                participant_handles=[_HANDLE_A, _HANDLE_B, _HANDLE_C],
            )
        ]
    )

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    export_str = json.dumps(ledger.export_ledger())
    for handle in (_HANDLE_A, _HANDLE_B, _HANDLE_C):
        assert handle not in export_str


# ---------------------------------------------------------------------------
# 8. no GHL write client reachable from this module
# ---------------------------------------------------------------------------


def test_no_ghl_write_client_referenced():
    source = Path(inspect.getsourcefile(imessage_collector)).read_text(encoding="utf-8")
    assert "ghl_reader" not in source
    assert "GhlContactReader" not in source
    assert "prepare_activation" not in source
    assert "confirm_activation" not in source
    assert "registry.json" not in source


# ---------------------------------------------------------------------------
# 9. one extraction per invocation
# ---------------------------------------------------------------------------


def test_one_extraction_per_invocation_zero_events(tmp_ledger):
    ledger, reg = tmp_ledger
    runner = _RunnerSpy([])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert runner.call_count == 1


# ---------------------------------------------------------------------------
# 10. source_event_id is read from the raw event and recorded as the stable
#    ledger source event id, with no dependence on message_guid
# ---------------------------------------------------------------------------


def test_source_event_id_recorded_as_stable_ledger_id(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    runner = _RunnerSpy([_make_event(source_event_id="apple-guid-xyz")])

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].source_event_id == "apple-guid-xyz"


def test_group_chat_source_event_id_suffixed_per_participant(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    runner = _RunnerSpy(
        [
            _make_event(
                source_event_id="apple-guid-group",
                is_from_me=True,
                is_group_chat=True,
                chat_guid="chat-group-x",
                handle_raw=None,
                participant_handles=[_HANDLE_A, _HANDLE_B],
            )
        ]
    )

    collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    row_a = ledger.query_events(_CONTACT_A)[0]
    row_b = ledger.query_events(_CONTACT_B)[0]
    assert row_a.source_event_id == "apple-guid-group:0"
    assert row_b.source_event_id == "apple-guid-group:1"


def test_no_dependence_on_message_guid():
    source = Path(inspect.getsourcefile(imessage_collector)).read_text(encoding="utf-8")
    assert "message_guid" not in source


def test_raw_event_without_message_guid_key_is_admitted(tmp_ledger):
    """A raw event carrying only source_event_id (no legacy message_guid key)
    must be admitted normally, proving the collector has no fallback read of
    message_guid.
    """
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    raw_event = {
        "source_event_id": "guid-no-legacy-key",
        "occurred_at": _EVENT_TS,
        "is_from_me": False,
        "is_group_chat": False,
        "chat_guid": "chat-1",
        "handle_raw": _HANDLE_A,
        "attachment_kinds": [],
        "has_attachments": False,
    }
    assert "message_guid" not in raw_event
    runner = _RunnerSpy([raw_event])

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert summary.admitted == 1
    rows = ledger.query_events(_CONTACT_A)
    assert rows[0].source_event_id == "guid-no-legacy-key"


def test_one_extraction_per_invocation_group_chat(tmp_ledger):
    ledger, reg = tmp_ledger
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    reg.set_allow(_HANDLE_C, _CONTACT_C)
    runner = _RunnerSpy(
        [
            _make_event(
                source_event_id="guid-group-3",
                is_from_me=True,
                is_group_chat=True,
                chat_guid="chat-group-3",
                handle_raw=None,
                participant_handles=[_HANDLE_A, _HANDLE_B, _HANDLE_C],
            )
        ]
    )

    summary = collect(reg, ledger, window_start=_WINDOW_START, window_end=_WINDOW_END, extract_runner=runner)

    assert runner.call_count == 1
    assert summary.admitted == 3
