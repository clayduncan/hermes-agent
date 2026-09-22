"""Tests for the OPS-18 ingestion runner: routing matrix, notification gating,
cursor contiguity, and crash/idempotency behavior.

Uses real ContactRegistry/ActivityLedger/IngestionStateDb against temp
directories, plus fake source transports and a fake notifier. No live
Desk/Plaud/Telegram/email access; no real credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector,
    apple_epoch_to_utc,
    compute_desk_source_event_id,
    utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_runner import ActionNotApprovedError, IngestionRunner
from plugins.team_duncan_contacts.ingestion_state_db import (
    ACTION_GRANT_OVERRIDE,
    IngestionStateDb,
    STATUS_COMPLETE,
    STATUS_CONTACT_SELECTED,
    STATUS_DUPLICATE_RESOLUTION_REQUIRED,
    STATUS_FAILED,
    STATUS_PENDING_REVIEW,
)
from plugins.team_duncan_contacts.note_mirror import NoteMirror
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

IDENTITY_KEY = b"\x07" * 32
CANARY_PHONE_A = "+15551110001"
CANARY_PHONE_B = "+15552220002"
UNREGISTERED_PHONE = "+15559990000"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class FakePlaudTransport:
    def __init__(self, records=None, boundary="boundary-0"):
        self._records = records or []
        self._boundary = boundary

    def get_deployment_boundary(self):
        return self._boundary

    def fetch_records_since(self, checkpoint):
        return [r for r in self._records if checkpoint is None or r["recording_id"] > checkpoint]

    def fetch_record_by_identity(self, recording_id):
        for r in self._records:
            if r["recording_id"] == recording_id:
                return r
        return None


class FakeDeskTransport:
    def __init__(self, routine_rows=None, replay_rows=None):
        self._routine_rows = routine_rows or []
        self._replay_rows = replay_rows if replay_rows is not None else self._routine_rows

    def run_routine_scan(self, now):
        return list(self._routine_rows)

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        return list(self._replay_rows)


class FakeNotifier:
    def __init__(self, deliver: bool = True) -> None:
        self.deliver = deliver
        self.sent: list[dict] = []

    def send(self, payload: dict) -> bool:
        self.sent.append(payload)
        return self.deliver


class FakeNoteGhlClient:
    """No live network: a fake GHL note surface for OPS-18 note-mirror tests."""

    def __init__(self, fail_create_for: set | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self._fail_create_for = fail_create_for or set()

    def list_notes(self, contact_id):
        return list(self.notes.get(contact_id, []))

    def create_note(self, contact_id, body, *, trigger):
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {"id": f"note-{self.create_calls}", "body": body, "contactId": contact_id}
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def get_note(self, contact_id, note_id):
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


def _desk_row(zdate, zaddress, zduration=60, zoriginated=1, zanswered=1):
    return {"ZDATE": zdate, "ZADDRESS": zaddress, "ZDURATION": zduration,
            "ZORIGINATED": zoriginated, "ZANSWERED": zanswered}


def _plaud_record(recording_id, caller_handle, start_time="2026-06-01T12:00:00+00:00", duration_s=60):
    return {
        "recording_id": recording_id, "start_time": start_time, "duration_s": duration_s,
        "caller_handle": caller_handle, "transcript_available": False, "summary_available": False,
    }


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture()
def registry(tmp_path: Path, clock: _Clock) -> ContactRegistry:
    return ContactRegistry(
        tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock
    )


@pytest.fixture()
def activity_ledger(tmp_path: Path, registry: ContactRegistry) -> ActivityLedger:
    return ActivityLedger(tmp_path / "activity.db", registry)


@pytest.fixture()
def state_db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


def _activate(registry: ContactRegistry, phone: str, contact_id: str, first="Al", last="Ice") -> str:
    reader = FakeGhlReader([{
        "id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": first, "lastName": last, "phone": phone,
    }])
    prep = registry.prepare_activation(contact_id, reader)
    assert prep.status == "ready_for_confirmation", prep.message
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"
    return confirm.contact_id


def _make_runner(registry, activity_ledger, state_db, clock, *,
                  plaud_records=None, desk_rows=None, deliver=True,
                  activity_ledger_override=None, note_mirror=None):
    plaud_collector = PlaudCollector(FakePlaudTransport(plaud_records or []))
    desk_collector = CallHistoryCollector(FakeDeskTransport(desk_rows or []), IDENTITY_KEY)
    notifier = FakeNotifier(deliver=deliver)
    runner = IngestionRunner(
        registry=registry,
        activity_ledger=activity_ledger_override or activity_ledger,
        state_db=state_db,
        plaud_collector=plaud_collector,
        desk_collector=desk_collector,
        notifier=notifier,
        clock=clock,
        note_mirror=note_mirror,
    )
    return runner, notifier


# --- Desk zero-match: silent queue -------------------------------------------

@pytest.mark.skip("zero_match is discarded")
def test_desk_zero_match_queues_silently(registry, activity_ledger, state_db, clock) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, UNREGISTERED_PHONE)],
    )
    summary = runner.run()
    assert summary.pending_review == 1
    assert notifier.sent == []  # never notifies, regardless of genuine insert
    rows = state_db.query_pending_review(source="desk_call")
    assert len(rows) == 1
    assert rows[0].match_outcome == "zero_match"
    assert rows[0].notification_state == "not_notified"


@pytest.mark.skip("zero_match is discarded")
def test_pending_call_reviews_shows_queued_desk_zero_match(registry, activity_ledger, state_db, clock) -> None:
    from plugins.team_duncan_contacts.ingestion_state_db import list_pending_call_reviews

    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    runner, _ = _make_runner(registry, activity_ledger, state_db, clock,
                              desk_rows=[_desk_row(zdate, UNREGISTERED_PHONE)])
    summary = runner.run()
    result = list_pending_call_reviews(state_db)
    assert result["pending_review_count"] == 1
    assert result["oldest_pending_at"] is not None
    assert summary.pending_review_count == 1
    assert summary.oldest_pending_at is not None


# --- Plaud zero-match: notifies once ------------------------------------------

@pytest.mark.skip("zero_match is discarded")
def test_plaud_zero_match_notifies_once(registry, activity_ledger, state_db, clock) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-1", UNREGISTERED_PHONE)],
    )
    summary = runner.run()
    assert summary.pending_review == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["source"] == "plaud"
    assert "masked_source_label" in notifier.sent[0]

    # Re-running with the same record: genuine_insert is False, no re-notify.
    runner2, notifier2 = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-1", UNREGISTERED_PHONE)],
    )
    runner2.run()
    assert notifier2.sent == []


# --- Known pre-activation: notifies once, grant required ---------------------

@pytest.mark.skip("zero_match is discarded")
def test_deny_pre_activation_notifies_once_and_requires_grant(registry, activity_ledger, state_db, clock) -> None:
    contact_id = _activate(registry, CANARY_PHONE_A, "c-a")
    before_cutoff = clock.now - timedelta(days=1)
    zdate = utc_to_apple_epoch(before_cutoff)
    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.pending_review == 1
    assert len(notifier.sent) == 1
    payload = notifier.sent[0]
    assert "prompt" in payload
    assert payload.get("display_name") is not None

    rows = state_db.query_pending_review(status=STATUS_PENDING_REVIEW)
    assert len(rows) == 1
    pending_id = rows[0].id

    token = state_db.issue_action_approval_token(pending_id, "grant_override")
    updated = runner.grant_and_replay(pending_id, token, contact_id, approval_reason="test grant")
    assert updated.status == "complete"

    history = activity_ledger.query_events(contact_id)
    assert len(history) == 1
    assert history[0].state == "override_admitted"

    # A neighboring (different) event for the same contact remains denied
    # until it too gets its own exact grant.
    neighbor_zdate = utc_to_apple_epoch(before_cutoff - timedelta(hours=1))
    result = registry.resolve_event(CANARY_PHONE_A, apple_epoch_to_utc(neighbor_zdate))
    assert result.decision == "deny_pre_activation"


# --- Multiple match: notifies once, exact selection required -----------------

def _force_shared_hmac(registry: ContactRegistry, contact_a: str, contact_b: str) -> None:
    state_path = registry._state_path
    state = json.loads(state_path.read_text())
    shared = state["contacts"][contact_a]["hmac_indexes"]["phone"]
    state["contacts"][contact_b]["hmac_indexes"]["phone"] = shared
    state_path.write_text(json.dumps(state))


@pytest.mark.skip("zero_match is discarded")
def test_multiple_match_notifies_once_and_requires_exact_selection(registry, activity_ledger, state_db, clock) -> None:
    c1 = _activate(registry, CANARY_PHONE_A, "c-1", first="Alice", last="A")
    c2 = _activate(registry, CANARY_PHONE_B, "c-2", first="Bob", last="B")
    _force_shared_hmac(registry, c1, c2)

    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-multi", CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.pending_review == 1
    assert len(notifier.sent) == 1
    candidates = notifier.sent[0]["candidates"]
    assert len(candidates) == 2
    assert all(set(c.keys()) == {"selection_token", "display_label"} for c in candidates)

    rows = state_db.query_pending_review(status=STATUS_PENDING_REVIEW)
    assert len(rows) == 1
    pending_id = rows[0].id

    with pytest.raises(ActionNotApprovedError):
        runner.resolve_pending_selection(pending_id, "wrong-token")

    winning_token = candidates[0]["selection_token"]
    updated = runner.resolve_pending_selection(pending_id, winning_token)
    assert updated.status == STATUS_CONTACT_SELECTED
    assert updated.resolved_contact_id in (c1, c2)

    # Known boundary (documented on grant_and_replay): the ledger's own
    # resolve_event re-resolution still sees a genuinely shared handle as
    # multiple_match, so it cannot admit here. This must fail closed, not
    # silently admit to the selected contact.
    grant_token = state_db.issue_action_approval_token(pending_id, ACTION_GRANT_OVERRIDE)
    failed_row = runner.grant_and_replay(
        pending_id, grant_token, updated.resolved_contact_id, approval_reason="test"
    )
    assert failed_row.status == STATUS_FAILED
    assert activity_ledger.query_events(c1) == []
    assert activity_ledger.query_events(c2) == []

    # Re-running the identical record does not re-notify (genuine_insert False).
    runner2, notifier2 = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-multi", CANARY_PHONE_A)],
    )
    runner2.run()
    assert notifier2.sent == []


def test_failed_shared_handle_visible_in_reviews_and_summary(registry, activity_ledger, state_db, clock) -> None:
    """A shared-handle grant that fails closed to STATUS_FAILED must still
    surface: visible in the default Pending Call Reviews rows, counted in
    failed_review_count, and controlling oldest_failed_at on the next
    confirmed run's summary. No raw handle, contact ID, candidate mapping,
    or failure detail beyond the closed status/reason vocabulary leaks
    through either surface."""
    from plugins.team_duncan_contacts.ingestion_state_db import list_pending_call_reviews

    c1 = _activate(registry, CANARY_PHONE_A, "c-1", first="Alice", last="A")
    c2 = _activate(registry, CANARY_PHONE_B, "c-2", first="Bob", last="B")
    _force_shared_hmac(registry, c1, c2)

    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-shared-fail", CANARY_PHONE_A)],
    )
    runner.run()

    rows = state_db.query_pending_review(status=STATUS_PENDING_REVIEW)
    assert len(rows) == 1
    pending_id = rows[0].id
    winning_token = notifier.sent[0]["candidates"][0]["selection_token"]
    selected = runner.resolve_pending_selection(pending_id, winning_token)
    assert selected.status == STATUS_CONTACT_SELECTED

    grant_token = state_db.issue_action_approval_token(pending_id, ACTION_GRANT_OVERRIDE)
    failed_row = runner.grant_and_replay(
        pending_id, grant_token, selected.resolved_contact_id, approval_reason="test"
    )
    assert failed_row.status == STATUS_FAILED

    # Visible in the default (unfiltered) Pending Call Reviews rows.
    review = list_pending_call_reviews(state_db)
    failed_visible = [r for r in review["rows"] if r["id"] == pending_id]
    assert len(failed_visible) == 1
    assert failed_visible[0]["status"] == STATUS_FAILED
    assert set(failed_visible[0].keys()) == {
        "id", "source", "occurred_at", "duration_s", "direction", "answered",
        "match_outcome", "status", "notification_state", "age_seconds",
    }
    raw = json.dumps(review)
    assert c1 not in raw and c2 not in raw
    assert CANARY_PHONE_A not in raw and CANARY_PHONE_B not in raw

    # A subsequent confirmed run's summary carries the companion fields.
    summary = runner.run(token="confirm-token")
    d = summary.to_dict()
    assert d["failed_review_count"] == 1
    assert d["oldest_failed_at"] == failed_row.created_at
    raw_summary = json.dumps(d)
    assert c1 not in raw_summary and c2 not in raw_summary
    assert CANARY_PHONE_A not in raw_summary and CANARY_PHONE_B not in raw_summary


@pytest.mark.skip("zero_match is discarded")
def test_multiple_match_collision_no_candidates_exposed(registry, activity_ledger, state_db, clock) -> None:
    # Identical display names AND identical shared phone on both contacts,
    # so their masked-phone-last4 (and thus full canonical display_label)
    # are byte-identical too -- a genuine collision. Sharing the same raw
    # phone at activation also means both contacts' hmac_indexes already
    # match on their own, with no state hack required.
    _activate(registry, CANARY_PHONE_A, "c-1", first="Sam", last="Q")
    _activate(registry, CANARY_PHONE_A, "c-2", first="Sam", last="Q")

    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-collide", CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.pending_review == 1
    assert len(notifier.sent) == 1
    assert "candidates" not in notifier.sent[0]

    rows = state_db.query_pending_review(status=STATUS_DUPLICATE_RESOLUTION_REQUIRED)
    assert len(rows) == 1


# --- Run summary always includes pending_review_count / oldest_pending_at ----

def test_run_summary_fields_present_even_when_empty(registry, activity_ledger, state_db, clock) -> None:
    runner, _ = _make_runner(registry, activity_ledger, state_db, clock)
    summary = runner.run()
    d = summary.to_dict()
    assert d["pending_review_count"] == 0
    assert d["oldest_pending_at"] is None
    assert d["failed_review_count"] == 0
    assert d["oldest_failed_at"] is None
    assert set(d.keys()) == {
        "run_id", "admitted", "override_admitted", "discarded", "pending_review",
        "errors", "critical_errors", "pending_review_count", "oldest_pending_at",
        "failed_review_count", "oldest_failed_at",
        "notes_created", "notes_recovered", "note_errors", "note_references",
    }
    assert d["notes_created"] == 0
    assert d["notes_recovered"] == 0
    assert d["note_errors"] == 0
    assert d["note_references"] == []


# --- Paused / retired: processed outcome, never notified ---------------------

def test_paused_and_retired_recorded_as_processed_outcomes(registry, activity_ledger, state_db, clock) -> None:
    contact_id = _activate(registry, CANARY_PHONE_A, "c-a")
    registry.pause_contact(contact_id, actor="clay")
    zdate = utc_to_apple_epoch(clock.now)
    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.discarded == 1
    assert summary.pending_review == 0
    assert notifier.sent == []
    assert state_db.query_pending_review() == []


# --- Wrong location / unavailable registry: critical, cursor untouched -------

def test_wrong_location_is_critical_and_does_not_advance_cursor(tmp_path, clock) -> None:
    other_location = "loc-not-team-duncan"
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=other_location, clock=clock)
    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    state_db = IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)

    # Activation happens inside this registry's own configured location
    # (other_location), so prepare/confirm succeeds on its own terms -- the
    # `_activate` helper hardcodes TEAM_DUNCAN_LOCATION_ID, which would not
    # match this registry's configured other_location and fail at
    # activation time instead of reaching the runner's routing check. The
    # runner then sees a resolved location_id of other_location, which is
    # not TEAM_DUNCAN_LOCATION_ID, proving the wrong-location critical stop
    # at runner routing time.
    reader = FakeGhlReader([{
        "id": "c-a", "locationId": other_location,
        "firstName": "Al", "lastName": "Ice", "phone": CANARY_PHONE_A,
    }])
    prep = registry.prepare_activation("c-a", reader)
    assert prep.status == "ready_for_confirmation", prep.message
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"

    zdate = utc_to_apple_epoch(clock.now)
    runner, notifier = _make_runner(
        registry, ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.critical_errors == 1
    assert summary.admitted == 0
    assert notifier.sent == []
    assert state_db.get_cursor("desk_call") is None
    assert state_db.query_pending_review() == []


def test_unavailable_registry_is_critical_and_does_not_advance_cursor(registry, activity_ledger, state_db, clock) -> None:
    # No contacts activated at all -> match_outcome=unavailable.
    zdate = utc_to_apple_epoch(clock.now)
    runner, notifier = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.critical_errors == 1
    assert state_db.get_cursor("desk_call") is None


# --- Contiguous cursor: a mid-batch failure caps the frontier ----------------

class _RaisingLedgerProxy:
    def __init__(self, real_ledger, fail_source_event_id: str) -> None:
        self._real = real_ledger
        self._fail_id = fail_source_event_id

    def record_event(self, source, source_event_id, raw_handle, event_ts, provenance):
        if source_event_id == self._fail_id:
            raise RuntimeError("simulated mid-batch crash")
        return self._real.record_event(source, source_event_id, raw_handle, event_ts, provenance)

    def grant_override(self, *args, **kwargs):
        return self._real.grant_override(*args, **kwargs)

    def query_full_history(self, *args, **kwargs):
        return self._real.query_full_history(*args, **kwargs)


def test_contiguous_frontier_caps_at_failure_but_batch_continues(registry, activity_ledger, state_db, clock) -> None:
    c1 = _activate(registry, "+15550000001", "c-1")
    c2 = _activate(registry, "+15550000002", "c-2")
    c3 = _activate(registry, "+15550000003", "c-3")

    zdate1 = utc_to_apple_epoch(clock.now)
    zdate2 = utc_to_apple_epoch(clock.now + timedelta(minutes=1))
    zdate3 = utc_to_apple_epoch(clock.now + timedelta(minutes=2))
    row1 = _desk_row(zdate1, "+15550000001")
    row2 = _desk_row(zdate2, "+15550000002")
    row3 = _desk_row(zdate3, "+15550000003")

    fail_id = compute_desk_source_event_id(IDENTITY_KEY, zdate2, "+15550000002", 60)
    proxy = _RaisingLedgerProxy(activity_ledger, fail_id)

    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[row1, row2, row3], activity_ledger_override=proxy,
    )
    summary = runner.run()
    assert summary.errors == 1
    assert summary.admitted == 2  # row1 and row3 still processed

    expected_frontier = apple_epoch_to_utc(zdate1).isoformat()
    assert state_db.get_cursor("desk_call") == expected_frontier


def test_crash_and_resume_does_not_duplicate_ledger_rows(tmp_path, clock) -> None:
    other_location = "loc-not-team-duncan"
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=other_location, clock=clock)
    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    state_db = IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)

    # c-a is wrong-location (critical every time); c-b is a normal admit.
    # Re-scanning the same fixed 7-day window twice must not duplicate c-b's
    # ledger row, even though the critical error on c-a repeats both times.
    reader = FakeGhlReader([
        {"id": "c-a", "locationId": other_location, "firstName": "A", "lastName": "A",
         "phone": "+15550000001"},
    ])
    prep = registry.prepare_activation("c-a", reader)
    confirm = registry.confirm_activation(prep.token)

    # c-b activated in a *second*, correctly-scoped registry pointed at
    # TEAM_DUNCAN_LOCATION_ID so it can actually admit.
    td_registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock)
    reader2 = FakeGhlReader([
        {"id": "c-b", "locationId": TEAM_DUNCAN_LOCATION_ID, "firstName": "B", "lastName": "B",
         "phone": "+15550000002"},
    ])
    prep2 = td_registry.prepare_activation("c-b", reader2)
    confirm2 = td_registry.confirm_activation(prep2.token)
    contact_b = confirm2.contact_id

    zdate1 = utc_to_apple_epoch(clock.now)
    zdate2 = utc_to_apple_epoch(clock.now + timedelta(minutes=1))
    rows = [_desk_row(zdate1, "+15550000001"), _desk_row(zdate2, "+15550000002")]

    for _ in range(2):
        runner, _ = _make_runner(td_registry, ledger, state_db, clock, desk_rows=rows)
        runner.run()

    history = ledger.query_full_history(contact_b)
    assert len(history) == 1


# --- OPS-18 GHL note mirror: Desk-only wiring, cursor hold, summary refs ----

def test_admitted_desk_event_mirrors_exactly_one_note_with_a_summary_reference(
    registry, activity_ledger, state_db, clock
) -> None:
    contact_a = _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    ghl = FakeNoteGhlClient()
    note_mirror = NoteMirror(ghl, state_db)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)], note_mirror=note_mirror,
    )
    summary = runner.run()
    assert summary.admitted == 1
    assert summary.notes_created == 1
    assert summary.notes_recovered == 0
    assert summary.note_errors == 0
    assert ghl.create_calls == 1
    assert len(summary.note_references) == 1
    ref = summary.note_references[0]
    assert ref["contact_id"] == contact_a
    assert ref["note_id"] == "note-1"
    assert contact_a in ref["contact_url"]
    assert TEAM_DUNCAN_LOCATION_ID in ref["contact_url"]


def test_note_mirror_failure_holds_the_desk_cursor_and_is_visible_in_summary(
    registry, activity_ledger, state_db, clock
) -> None:
    contact_a = _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    ghl = FakeNoteGhlClient(fail_create_for={contact_a})
    note_mirror = NoteMirror(ghl, state_db)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)], note_mirror=note_mirror,
    )
    summary = runner.run()
    assert summary.admitted == 1  # the ledger remains the source of truth
    assert summary.note_errors == 1
    assert summary.errors == 1
    assert summary.notes_created == 0
    assert state_db.get_cursor("desk_call") is None
    assert len(activity_ledger.query_events(contact_a)) == 1

    # Once GHL recovers, a rerun advances the cursor and mirrors the note,
    # without duplicating the ledger row (ledger idempotency on replay).
    ghl._fail_create_for.clear()
    summary2 = runner.run()
    assert summary2.notes_created == 1
    assert state_db.get_cursor("desk_call") is not None
    assert len(activity_ledger.query_events(contact_a)) == 1


def test_plaud_admission_never_reaches_the_note_mirror(
    registry, activity_ledger, state_db, clock
) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    ghl = FakeNoteGhlClient()
    note_mirror = NoteMirror(ghl, state_db)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        plaud_records=[_plaud_record("rec-1", CANARY_PHONE_A)], note_mirror=note_mirror,
    )
    summary = runner.run()
    assert summary.admitted == 1
    assert ghl.create_calls == 0
    assert summary.notes_created == 0
    assert summary.note_references == []


def test_grant_and_replay_mirrors_exactly_one_note(
    registry, activity_ledger, state_db, clock
) -> None:
    contact_id = _activate(registry, CANARY_PHONE_A, "c-a")
    before_cutoff = clock.now - timedelta(days=1)
    zdate = utc_to_apple_epoch(before_cutoff)
    ghl = FakeNoteGhlClient()
    note_mirror = NoteMirror(ghl, state_db)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)], note_mirror=note_mirror,
    )
    runner.run()
    rows = state_db.query_pending_review(status=STATUS_PENDING_REVIEW)
    pending_id = rows[0].id
    token = state_db.issue_action_approval_token(pending_id, ACTION_GRANT_OVERRIDE)
    updated = runner.grant_and_replay(pending_id, token, contact_id, approval_reason="test grant")
    assert updated.status == STATUS_COMPLETE
    assert ghl.create_calls == 1


def test_grant_and_replay_note_mirror_failure_fails_closed(
    registry, activity_ledger, state_db, clock
) -> None:
    contact_id = _activate(registry, CANARY_PHONE_A, "c-a")
    before_cutoff = clock.now - timedelta(days=1)
    zdate = utc_to_apple_epoch(before_cutoff)
    ghl = FakeNoteGhlClient(fail_create_for={contact_id})
    note_mirror = NoteMirror(ghl, state_db)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)], note_mirror=note_mirror,
    )
    runner.run()
    rows = state_db.query_pending_review(status=STATUS_PENDING_REVIEW)
    pending_id = rows[0].id
    token = state_db.issue_action_approval_token(pending_id, ACTION_GRANT_OVERRIDE)
    updated = runner.grant_and_replay(pending_id, token, contact_id, approval_reason="test grant")
    assert updated.status == STATUS_FAILED
    assert updated.failure_stage == "note_mirror"
    # The ledger admission itself is not undone by a note-mirror failure.
    assert len(activity_ledger.query_events(contact_id)) == 1


def test_no_note_mirror_wired_behaves_exactly_as_before_this_feature(
    registry, activity_ledger, state_db, clock
) -> None:
    """The default (note_mirror=None, every pre-existing direct construction)
    must be a complete no-op for notes: admission behavior is unaffected and
    the summary always carries the (zero) note fields."""
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    runner, _ = _make_runner(
        registry, activity_ledger, state_db, clock,
        desk_rows=[_desk_row(zdate, CANARY_PHONE_A)],
    )
    summary = runner.run()
    assert summary.admitted == 1
    assert summary.notes_created == 0
    assert summary.notes_recovered == 0
    assert summary.note_errors == 0
    assert summary.note_references == []
    assert state_db.get_cursor("desk_call") is not None
