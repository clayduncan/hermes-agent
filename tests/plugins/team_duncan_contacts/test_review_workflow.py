"""Tests for the OPS-18 review workflow: sealed re-fetch, contact creation,
separate activation prepare/confirm, missing/ambiguous re-fetch, and dismiss.

Real ContactRegistry/ActivityLedger/IngestionStateDb against temp
directories; fake source transports. No live access anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector, apple_epoch_to_utc, compute_desk_source_event_id, utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_runner import ActionNotApprovedError, IngestionRunner
from plugins.team_duncan_contacts.ingestion_state_db import (
    ACTION_CREATE_CONTACT,
    IngestionStateDb,
    STATUS_AWAITING_ACTIVATION_CONFIRMATION,
    STATUS_CONTACT_ACTIVATED,
    STATUS_CONTACT_CREATED,
    STATUS_DISMISSED,
    STATUS_PENDING_REVIEW,
    STATUS_SOURCE_AMBIGUOUS,
    STATUS_SOURCE_MISSING,
    creation_idempotency_key,
)
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

UNREGISTERED_PHONE = "+15559990000"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class FakePlaudTransport:
    def __init__(self, records=None):
        self._records = {r["recording_id"]: r for r in (records or [])}

    def get_deployment_boundary(self):
        return "boundary-0"

    def fetch_records_since(self, checkpoint):
        return list(self._records.values())

    def fetch_record_by_identity(self, recording_id):
        return self._records.get(recording_id)

    def remove(self, recording_id: str) -> None:
        self._records.pop(recording_id, None)


class FakeDeskTransport:
    def __init__(self, routine_rows=None, replay_rows=None):
        self._routine_rows = routine_rows or []
        self._replay_rows = replay_rows if replay_rows is not None else self._routine_rows

    def run_routine_scan(self, now):
        return list(self._routine_rows)

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        return list(self._replay_rows)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return True


def _plaud_record(recording_id, caller_handle, start_time="2026-06-01T09:00:00+00:00"):
    return {
        "recording_id": recording_id, "start_time": start_time, "duration_s": 45,
        "caller_handle": caller_handle, "transcript_available": False, "summary_available": False,
    }


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture()
def registry(tmp_path: Path, clock: _Clock) -> ContactRegistry:
    return ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock)


@pytest.fixture()
def activity_ledger(tmp_path: Path, registry: ContactRegistry) -> ActivityLedger:
    return ActivityLedger(tmp_path / "activity.db", registry)


@pytest.fixture()
def state_db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


IDENTITY_KEY = b"\x09" * 32


@pytest.fixture()
def plaud_transport() -> FakePlaudTransport:
    return FakePlaudTransport([_plaud_record("rec-zero", UNREGISTERED_PHONE)])


@pytest.fixture(autouse=True)
def _baseline_activated_contact(registry: ContactRegistry) -> None:
    """One unrelated activated contact, so a non-matching handle produces a
    genuine zero_match rather than match_outcome=unavailable (empty registry)."""
    reader = FakeGhlReader([{
        "id": "c-baseline", "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Baseline", "lastName": "Contact", "phone": "+15550000000",
    }])
    prep = registry.prepare_activation("c-baseline", reader)
    registry.confirm_activation(prep.token)


@pytest.fixture()
def runner(registry, activity_ledger, state_db, clock, plaud_transport):
    desk_collector = CallHistoryCollector(FakeDeskTransport([]), IDENTITY_KEY)
    return IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=PlaudCollector(plaud_transport), desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )


@pytest.fixture()
def zero_match_pending_id(state_db) -> str:
    """The runner no longer auto-queues a zero_match encountered during
    ingestion (OPS-18: zero_match is discarded, never queued). The rest of
    the review workflow under test here -- sealed creation, activation,
    dismiss -- is untouched by that change and still operates on a
    pending_review row exactly like this one, so this directly constructs
    the legacy row via the same insert_pending_review() API the runner
    itself still uses for deny_pre_activation/multiple_match rows."""
    ins = state_db.insert_pending_review(
        source="plaud", source_event_id="rec-zero", decision="review_required",
        match_outcome="zero_match", occurred_at="2026-06-01T09:00:00+00:00",
        duration_s=45, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    return ins.pending_review_id


def test_creation_requires_valid_approval_token(runner, state_db, zero_match_pending_id) -> None:
    calls = []

    def perform_create(raw_handle):
        calls.append(raw_handle)
        return {"id": "c-new"}

    with pytest.raises(ActionNotApprovedError):
        runner.with_sealed_creation_handle(zero_match_pending_id, "not-a-real-token", perform_create)
    assert calls == []


def test_creation_sealed_refetch_read_before_create_and_idempotency_key(
    runner, state_db, zero_match_pending_id
) -> None:
    seen_handles = []

    def perform_create(raw_handle):
        seen_handles.append(raw_handle)
        return {"id": "c-new"}

    token = state_db.issue_action_approval_token(zero_match_pending_id, ACTION_CREATE_CONTACT)
    row = runner.with_sealed_creation_handle(zero_match_pending_id, token, perform_create)

    assert seen_handles == [UNREGISTERED_PHONE]  # sealed re-fetch handed the exact raw handle
    assert row.status == STATUS_CONTACT_CREATED
    assert row.resolved_contact_id == "c-new"

    stored = state_db.get_pending_review(zero_match_pending_id)
    assert stored.idempotency_key == creation_idempotency_key("plaud", stored.source_event_id)

    # Creation never activates automatically.
    assert stored.status == STATUS_CONTACT_CREATED


def test_creation_token_is_single_use(runner, state_db, zero_match_pending_id) -> None:
    token = state_db.issue_action_approval_token(zero_match_pending_id, ACTION_CREATE_CONTACT)
    runner.with_sealed_creation_handle(zero_match_pending_id, token, lambda h: {"id": "c-new"})
    with pytest.raises(ActionNotApprovedError):
        # Row is no longer pending_review/zero_match, so even a fresh token
        # for this action would be rejected by the eligibility check -- but
        # this reuses the *same* (already-consumed) token, which must fail
        # on its own regardless.
        runner.with_sealed_creation_handle(zero_match_pending_id, token, lambda h: {"id": "c-other"})


def test_activation_is_separate_prepare_and_confirm(
    runner, state_db, registry, clock, zero_match_pending_id
) -> None:
    row = runner.with_sealed_creation_handle(
        zero_match_pending_id,
        state_db.issue_action_approval_token(zero_match_pending_id, ACTION_CREATE_CONTACT),
        lambda h: {"id": "c-new"},
    )
    assert row.status == STATUS_CONTACT_CREATED

    ghl_reader = FakeGhlReader([{
        "id": "c-new", "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "New", "lastName": "Contact", "phone": UNREGISTERED_PHONE,
    }])
    prepare_result = runner.prepare_new_contact_activation(zero_match_pending_id, ghl_reader)
    assert prepare_result.status == "ready_for_confirmation"

    row = state_db.get_pending_review(zero_match_pending_id)
    assert row.status == STATUS_AWAITING_ACTIVATION_CONFIRMATION
    # Still not activated in the registry itself.
    resolve_before = registry.resolve_event(UNREGISTERED_PHONE, clock.now)
    assert resolve_before.match_outcome == "zero_match"

    confirm_result = runner.confirm_new_contact_activation(zero_match_pending_id, prepare_result.token)
    assert confirm_result.status == "activated"

    row = state_db.get_pending_review(zero_match_pending_id)
    assert row.status == STATUS_CONTACT_ACTIVATED

    clock.advance(seconds=1)
    resolve_after = registry.resolve_event(UNREGISTERED_PHONE, clock.now)
    assert resolve_after.match_outcome == "unique_match"


def test_missing_source_refetch_fails_visibly_no_creation(runner, state_db, plaud_transport, zero_match_pending_id) -> None:
    plaud_transport.remove("rec-zero")
    calls = []

    def perform_create(raw_handle):
        calls.append(raw_handle)
        return {"id": "should-not-be-created"}

    token = state_db.issue_action_approval_token(zero_match_pending_id, ACTION_CREATE_CONTACT)
    row = runner.with_sealed_creation_handle(zero_match_pending_id, token, perform_create)
    assert row.status == STATUS_SOURCE_MISSING
    assert calls == []  # never reached create


def test_ambiguous_desk_replay_fails_visibly_no_creation(registry, activity_ledger, state_db, clock) -> None:
    # A desk zero_match is no longer auto-queued by runner.run() (OPS-18:
    # discarded, never queued), so the legacy pending_review row this test
    # needs is constructed directly -- with a source_event_id computed the
    # exact same way the collector itself would, so the later exact-event
    # replay lookup still recognizes both returned rows as genuine matches
    # for it (which is what makes the lookup ambiguous rather than missing).
    occurred_at = clock.now - timedelta(days=1)
    zdate = utc_to_apple_epoch(occurred_at)
    row = {"ZDATE": zdate, "ZADDRESS": UNREGISTERED_PHONE, "ZDURATION": 30, "ZORIGINATED": 1, "ZANSWERED": 1}
    source_event_id = compute_desk_source_event_id(IDENTITY_KEY, zdate, UNREGISTERED_PHONE, 30)
    desk_transport = FakeDeskTransport(routine_rows=[row], replay_rows=[row, dict(row)])
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=PlaudCollector(FakePlaudTransport([])),
        desk_collector=CallHistoryCollector(desk_transport, IDENTITY_KEY),
        notifier=FakeNotifier(), clock=clock,
    )
    ins = state_db.insert_pending_review(
        source="desk_call", source_event_id=source_event_id, decision="review_required",
        match_outcome="zero_match", occurred_at=occurred_at.isoformat(),
        duration_s=30, direction="outbound", answered=1, status=STATUS_PENDING_REVIEW,
    )
    pending_id = ins.pending_review_id

    calls = []
    token = state_db.issue_action_approval_token(pending_id, ACTION_CREATE_CONTACT)
    result = runner.with_sealed_creation_handle(pending_id, token, lambda h: calls.append(h) or {"id": "x"})
    assert result.status == STATUS_SOURCE_AMBIGUOUS
    assert calls == []


def test_dismiss(runner, state_db, zero_match_pending_id) -> None:
    row = runner.dismiss_pending_review(zero_match_pending_id, "not a real lead")
    assert row.status == STATUS_DISMISSED
    stored = state_db.get_pending_review(zero_match_pending_id)
    assert stored.status == STATUS_DISMISSED
