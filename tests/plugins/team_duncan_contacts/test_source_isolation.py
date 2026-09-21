"""OPS-18 regression tests: Plaud source failures (unconfigured transport,
or any transport exception) must stay local to `_run_plaud` and never
prevent `_run_desk` from running, and vice versa for a Desk-side failure.

Also covers the explicit enabled-source contract (Clay's source-isolation
correction): a Desk-only `enabled_sources` configuration must never call
into Plaud at all -- no cursor read, initialize, fetch, or error increment
-- rather than merely tolerating a Plaud failure inside a combined run.

Uses the same fake-transport/real-registry pattern as test_ingestion_runner.py;
no live Desk/Plaud/Telegram/email access, no real credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector,
    utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_runner import IngestionRunner
from plugins.team_duncan_contacts.ingestion_state_db import (
    IngestionStateDb,
    SOURCE_DESK_CALL,
    SOURCE_PLAUD,
)
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

IDENTITY_KEY = b"\x07" * 32
CANARY_PHONE_A = "+15551110001"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class _RaisingPlaudTransport:
    """Mirrors `_UnconfiguredTransport`: every method fails locally, in
    process, with no I/O -- and never even gets to a boundary value."""

    def get_deployment_boundary(self):
        raise RuntimeError("no live transport configured")

    def fetch_records_since(self, checkpoint):
        raise RuntimeError("no live transport configured")

    def fetch_record_by_identity(self, recording_id):
        raise RuntimeError("no live transport configured")


class _FetchRaisingPlaudTransport:
    """Cursor already initialized; only the fetch call fails."""

    def __init__(self, boundary: str) -> None:
        self._boundary = boundary

    def get_deployment_boundary(self):
        return self._boundary

    def fetch_records_since(self, checkpoint):
        raise RuntimeError("plaud fetch exploded")

    def fetch_record_by_identity(self, recording_id):
        raise RuntimeError("plaud fetch exploded")


class _CountingPlaudTransport:
    """Records every call so a Desk-only run can be asserted to reach zero
    of them -- not just to tolerate a failure from one of them."""

    def __init__(self) -> None:
        self.boundary_calls = 0
        self.fetch_calls = 0
        self.identity_calls = 0

    def get_deployment_boundary(self):
        self.boundary_calls += 1
        raise RuntimeError("Plaud must not be reached in a Desk-only run")

    def fetch_records_since(self, checkpoint):
        self.fetch_calls += 1
        raise RuntimeError("Plaud must not be reached in a Desk-only run")

    def fetch_record_by_identity(self, recording_id):
        self.identity_calls += 1
        raise RuntimeError("Plaud must not be reached in a Desk-only run")


class _RaisingDeskTransport:
    def run_routine_scan(self, now):
        raise RuntimeError("desk transport exploded")

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        raise RuntimeError("desk transport exploded")


class FakeDeskTransport:
    def __init__(self, routine_rows=None) -> None:
        self._routine_rows = routine_rows or []

    def run_routine_scan(self, now):
        return list(self._routine_rows)

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        return list(self._routine_rows)


class FakeNotifier:
    def send(self, payload: dict) -> bool:
        return True


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


def test_unconfigured_plaud_init_failure_is_local_and_desk_still_runs(
    registry, activity_ledger, state_db, clock
) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    plaud_collector = PlaudCollector(_RaisingPlaudTransport())
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run()

    assert summary.errors == 1
    assert summary.admitted == 1  # Desk still ran and admitted its record.


def test_plaud_cursor_unchanged_after_init_failure(registry, activity_ledger, state_db, clock) -> None:
    plaud_collector = PlaudCollector(_RaisingPlaudTransport())
    desk_collector = CallHistoryCollector(FakeDeskTransport([]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    runner.run()

    assert state_db.get_cursor(SOURCE_PLAUD) is None


def test_plaud_fetch_failure_after_existing_cursor_is_local_and_desk_still_runs(
    registry, activity_ledger, state_db, clock
) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    state_db.set_cursor(SOURCE_PLAUD, "boundary-existing")
    zdate = utc_to_apple_epoch(clock.now)
    plaud_collector = PlaudCollector(_FetchRaisingPlaudTransport("boundary-existing"))
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run()

    assert summary.errors == 1
    assert summary.admitted == 1  # Desk still ran and admitted its record.
    assert state_db.get_cursor(SOURCE_PLAUD) == "boundary-existing"  # unchanged


def test_desk_failure_is_local_and_does_not_erase_plaud_work(
    registry, activity_ledger, state_db, clock
) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    plaud_collector = PlaudCollector(_desk_free_plaud_transport(CANARY_PHONE_A))
    desk_collector = CallHistoryCollector(_RaisingDeskTransport(), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run()

    assert summary.errors == 1
    assert summary.admitted == 1  # Plaud's admission survives the Desk failure.
    assert state_db.get_cursor(SOURCE_DESK_CALL) is None


class _FixedPlaudTransport:
    """A record already sitting at/after the deployment boundary. Mirrors the
    real transport contract: get_deployment_boundary() returns a position,
    and fetch_records_since(checkpoint) returns every record at or after that
    position -- unlike the real _run_plaud flow, first-run init always
    resolves *checkpoint* to the boundary before the first fetch, so gating
    on `checkpoint is None` would silently drop this record every time."""

    _DEPLOYMENT_BOUNDARY = "2026-01-01T00:00:00+00:00"

    def __init__(self, record: dict) -> None:
        self._record = record

    def get_deployment_boundary(self):
        return self._DEPLOYMENT_BOUNDARY

    def fetch_records_since(self, checkpoint):
        if checkpoint is None or checkpoint <= self._record["start_time"]:
            return [self._record]
        return []

    def fetch_record_by_identity(self, recording_id):
        return self._record if self._record["recording_id"] == recording_id else None


def _desk_free_plaud_transport(phone: str) -> _FixedPlaudTransport:
    return _FixedPlaudTransport(_plaud_record("rec-desk-fail", phone))


def test_manual_run_records_summary_despite_one_source_failing(
    registry, activity_ledger, state_db, clock
) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    plaud_collector = PlaudCollector(_RaisingPlaudTransport())
    zdate = utc_to_apple_epoch(clock.now)
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run(token="manual-confirm-token")

    assert summary.run_id is not None
    stored = state_db.get_run(summary.run_id)
    assert stored is not None
    assert stored["errors"] == 1
    assert stored["admitted"] == 1


def test_normal_run_both_sources_succeed_unchanged(registry, activity_ledger, state_db, clock) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    plaud_collector = PlaudCollector(_FixedPlaudTransport(_plaud_record("rec-ok", CANARY_PHONE_A)))
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run()

    assert summary.errors == 0
    assert summary.admitted == 2
    assert state_db.get_cursor(SOURCE_PLAUD) == "2026-06-01T12:00:00+00:00"
    assert state_db.get_cursor(SOURCE_DESK_CALL) is not None


# ---------------------------------------------------------------------------
# Explicit enabled-source contract: Desk-only must never touch Plaud at all,
# not even to tolerate a failure from it.
# ---------------------------------------------------------------------------


def test_desk_only_run_never_calls_plaud_transport(registry, activity_ledger, state_db, clock) -> None:
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    plaud_transport = _CountingPlaudTransport()
    plaud_collector = PlaudCollector(plaud_transport)
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
        enabled_sources=frozenset({SOURCE_DESK_CALL}),
    )

    summary = runner.run()

    assert plaud_transport.boundary_calls == 0
    assert plaud_transport.fetch_calls == 0
    assert plaud_transport.identity_calls == 0
    assert summary.errors == 0  # No tolerated Plaud error either -- it was never attempted.
    assert summary.admitted == 1
    assert state_db.get_cursor(SOURCE_PLAUD) is None  # Never initialized.


def test_desk_only_run_records_no_plaud_cursor_mutation(registry, activity_ledger, state_db, clock) -> None:
    plaud_collector = PlaudCollector(_CountingPlaudTransport())
    desk_collector = CallHistoryCollector(FakeDeskTransport([]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
        enabled_sources=frozenset({SOURCE_DESK_CALL}),
    )

    summary = runner.run(token="manual-confirm-token")

    assert state_db.get_cursor(SOURCE_PLAUD) is None
    stored = state_db.get_run(summary.run_id)
    assert stored["errors"] == 0


def test_enabled_sources_defaults_to_both_for_backward_compatibility(
    registry, activity_ledger, state_db, clock
) -> None:
    """Direct construction without enabled_sources (as every pre-existing
    test in this suite does) must keep running both sources, unchanged."""
    _activate(registry, CANARY_PHONE_A, "c-a")
    zdate = utc_to_apple_epoch(clock.now)
    plaud_collector = PlaudCollector(_FixedPlaudTransport(_plaud_record("rec-both", CANARY_PHONE_A)))
    desk_collector = CallHistoryCollector(FakeDeskTransport([_desk_row(zdate, CANARY_PHONE_A)]), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
    )

    summary = runner.run()

    assert summary.admitted == 2
    assert state_db.get_cursor(SOURCE_PLAUD) is not None
    assert state_db.get_cursor(SOURCE_DESK_CALL) is not None


def test_plaud_only_enabled_sources_never_calls_desk_transport(
    registry, activity_ledger, state_db, clock
) -> None:
    """The isolation contract works symmetrically: an explicit Plaud-only
    configuration must never touch the Desk transport either."""
    _activate(registry, CANARY_PHONE_A, "c-a")
    plaud_collector = PlaudCollector(_desk_free_plaud_transport(CANARY_PHONE_A))
    desk_collector = CallHistoryCollector(_RaisingDeskTransport(), IDENTITY_KEY)
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=plaud_collector, desk_collector=desk_collector,
        notifier=FakeNotifier(), clock=clock,
        enabled_sources=frozenset({SOURCE_PLAUD}),
    )

    summary = runner.run()

    assert summary.errors == 0
    assert summary.admitted == 1
    assert state_db.get_cursor(SOURCE_DESK_CALL) is None
