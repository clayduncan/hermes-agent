"""Tests for the OPS-114 non-interactive automation entry point.

No live transport, no live GHL, no real registry config -- every mode is
exercised against fakes/monkeypatched factories, mirroring
test_trigger_tools.py's precedent. The flagship test
(``test_desk_mode_never_touches_confirmation_token_or_run_tables``) proves
the scheduled path's central safety property: it shares the ingestion
runner/state_db with the interactive tools but never creates or consumes
an interactive confirmation token or an awaiting-acceptance run row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import plugins.team_duncan_contacts as team_duncan_contacts
from plugins.team_duncan_contacts import automation_runner
from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector,
    utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.heartbeat import HeartbeatStore
from plugins.team_duncan_contacts.ingestion_runner import IngestionRunner
from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import NoteMirror
from plugins.team_duncan_contacts.process_lock import TeamDuncanLock
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

CANARY_PHONE = "+15556665555"
IDENTITY_KEY = b"\x0a" * 32


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class _DeskRowTransport:
    def __init__(self, rows):
        self._rows = rows

    def get_deployment_boundary(self):
        return "boundary-0"

    def fetch_records_since(self, checkpoint):
        return []

    def fetch_record_by_identity(self, recording_id):
        return None

    def run_routine_scan(self, now):
        return list(self._rows)

    def run_replay_lookup(self, **kwargs):
        return []


class _FakeNoteGhlClient:
    def __init__(self) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0

    def create_note(self, contact_id, body, *, trigger, color=None, pinned=False, title=None):
        self.create_calls += 1
        note = {"id": f"note-{self.create_calls}", "body": body}
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def update_note(self, contact_id, note_id, body, *, trigger, color=None, pinned=None, userId=None, title=None):
        raise AssertionError("update_note should not be needed in this test")

    def get_note(self, contact_id, note_id):
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


class _EmptyTransport:
    def get_deployment_boundary(self):
        return "boundary-0"

    def fetch_records_since(self, checkpoint):
        return []

    def fetch_record_by_identity(self, recording_id):
        return None

    def run_routine_scan(self, now):
        return []

    def run_replay_lookup(self, **kwargs):
        return []


class _NoopNotifier:
    def send(self, payload):
        return False


@dataclass
class _FakeSummary:
    counts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.counts)


class _FakeRunner:
    def __init__(self, summary: _FakeSummary | None = None, *, raises: Exception | None = None):
        self._summary = summary or _FakeSummary({"admitted": 0, "errors": 0})
        self._raises = raises
        self.run_calls: list[dict[str, Any]] = []
        self.process_one_calls: list[str] = []
        self.close_calls = 0

    def run(self, *, token=None):
        self.run_calls.append({"token": token})
        if self._raises is not None:
            raise self._raises
        return self._summary

    def process_one(self, plaud_recording_id: str):
        self.process_one_calls.append(plaud_recording_id)
        if self._raises is not None:
            raise self._raises
        return self._summary

    def close(self):
        self.close_calls += 1


@pytest.fixture()
def fake_registry_wiring(monkeypatch):
    """Bypasses config/startup validation entirely: build_registry_and_reader
    returns fixed fake objects, and the two runner factories return a
    caller-supplied _FakeRunner. Returns a small namespace test bodies use
    to inject the runner and inspect calls."""

    state = {"runner": _FakeRunner(), "registry_ok": True}

    def _fake_build_registry_and_reader(hermes_home):
        if not state["registry_ok"]:
            return None, None, None
        return object(), object(), "loc-1"

    def _fake_ingestion_factory(hermes_home, registry):
        return lambda: (state["runner"], object())

    def _fake_plaud_factory(hermes_home, registry, ghl_reader):
        return lambda: (state["runner"], object())

    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader", _fake_build_registry_and_reader
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_ingestion_runner_factory", _fake_ingestion_factory
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory", _fake_plaud_factory
    )
    return state


def test_desk_mode_completed(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"admitted": 1, "errors": 0}))
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].run_calls == [{"token": None}]


def test_plaud_reconcile_mode_completed(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"matched": 0, "errors": 0}))
    exit_code = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].run_calls == [{"token": None}]


def test_plaud_webhook_mode_processes_exact_recording_id(tmp_path: Path, fake_registry_wiring) -> None:
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-abc123"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].process_one_calls == ["rec-abc123"]


def test_plaud_webhook_mode_with_errors_is_failed_not_completed(
    tmp_path: Path, fake_registry_wiring
) -> None:
    """A Plaud summary that reports errors > 0 without raising must still be
    recorded and reported as a failed outcome -- otherwise a durable webhook
    queue row gets removed even though its recording was never actually
    processed."""
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"matched": 0, "errors": 1}))
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-err-1"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_FAILED

    state = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts").read("plaud_webhook")
    assert state["last_outcome"] == "failed"
    assert state["last_success_at"] is None


def test_plaud_webhook_mode_zero_errors_is_completed(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"matched": 1, "errors": 0}))
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-ok-1"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_COMPLETED

    state = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts").read("plaud_webhook")
    assert state["last_outcome"] == "completed"
    assert state["last_success_at"] is not None


def test_plaud_reconcile_mode_with_errors_is_failed(tmp_path: Path, fake_registry_wiring, capsys) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"matched": 0, "errors": 2}))
    exit_code = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "failed"
    assert payload["counts"]["errors"] == 2


def test_desk_mode_with_critical_errors_is_failed(tmp_path: Path, fake_registry_wiring) -> None:
    """Desk counts carry ``critical_errors`` separately from ``errors``; a
    run with zero ``errors`` but a nonzero ``critical_errors`` must still be
    treated as a failed outcome."""
    fake_registry_wiring["runner"] = _FakeRunner(
        _FakeSummary({"admitted": 0, "errors": 0, "critical_errors": 1})
    )
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    state = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts").read("desk")
    assert state["last_outcome"] == "failed"
    assert state["last_success_at"] is None


def test_registry_unavailable_is_failed(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["registry_ok"] = False
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    from plugins.team_duncan_contacts.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts")
    state = store.read("desk")
    assert state["last_outcome"] == "failed"
    assert state["error_class"] == "RegistryUnavailableError"
    assert state["last_success_at"] is None


def test_runner_exception_is_failed_and_lock_is_released(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(raises=RuntimeError("boom"))
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    from plugins.team_duncan_contacts.heartbeat import HeartbeatStore

    state = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts").read("desk")
    assert state["last_outcome"] == "failed"
    assert state["error_class"] == "RuntimeError"
    # No raw exception message anywhere in the recorded state.
    assert "boom" not in json.dumps(state)

    # Lock was released on the failure path -- a fresh acquire succeeds.
    lock = TeamDuncanLock(hermes_home=tmp_path)
    result = lock.acquire(mode="probe")
    assert result.acquired is True
    lock.release()


def test_lock_skip_never_calls_build_registry_and_reader(tmp_path: Path, fake_registry_wiring, monkeypatch) -> None:
    calls: list[str] = []

    def _spy(*args, **kwargs):
        calls.append("called")
        return object(), object(), "loc-1"

    monkeypatch.setattr(team_duncan_contacts, "build_registry_and_reader", _spy)

    holder = TeamDuncanLock(hermes_home=tmp_path)
    assert holder.acquire(mode="external-holder").acquired is True
    try:
        exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
        assert exit_code == automation_runner.EXIT_SKIPPED_LOCK
        assert calls == []

        from plugins.team_duncan_contacts.heartbeat import HeartbeatStore

        state = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts").read("desk")
        assert state["last_outcome"] == "skipped_lock"
        assert state["last_completed_at"] is None
        assert state["last_success_at"] is None
    finally:
        holder.release()


def test_desk_counts_are_sanitized_of_note_references(tmp_path: Path, fake_registry_wiring, capsys) -> None:
    raw = {
        "run_id": "run-1",
        "admitted": 1,
        "override_admitted": 0,
        "discarded": 0,
        "pending_review": 0,
        "errors": 0,
        "critical_errors": 0,
        "critical_error_reasons": [],
        "pending_review_count": 0,
        "oldest_pending_at": None,
        "failed_review_count": 0,
        "oldest_failed_at": None,
        "notes_created": 1,
        "notes_recovered": 0,
        "note_errors": 0,
        "note_references": [
            {"note_id": "n-1", "contact_id": "c-1", "contact_url": "https://example/c-1"}
        ],
    }
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary(raw))
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert "note_references" not in payload["counts"]
    assert "c-1" not in captured.out
    assert "https://example/c-1" not in captured.out


def test_cron_session_env_var_has_no_effect_on_automation_runner(
    tmp_path: Path, fake_registry_wiring, monkeypatch
) -> None:
    """The automation entry point never consults HERMES_CRON_SESSION -- that
    guard exists solely for the interactive prepare/confirm/accept tools."""
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].run_calls == [{"token": None}]


def test_output_is_single_json_line_with_no_secrets(tmp_path: Path, fake_registry_wiring, capsys) -> None:
    automation_runner.run(["desk"], hermes_home=tmp_path)
    out = capsys.readouterr().out.strip()
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 1
    json.loads(lines[0])  # must be valid JSON


# ---------------------------------------------------------------------------
# Flagship integration test: the scheduled path shares ingestion_state.db
# with the interactive tools but never touches the token/run bookkeeping
# tables those tools own.
# ---------------------------------------------------------------------------


def test_desk_mode_never_touches_confirmation_token_or_run_tables(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "plugin-data" / "team_duncan_contacts"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "ingestion_state.db"
    state_db = IngestionStateDb(db_path)
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID)
    ledger = ActivityLedger(tmp_path / "activity.db", registry)

    def _fake_build_registry_and_reader(hermes_home):
        return registry, object(), "loc-1"

    def _fake_ingestion_factory(hermes_home, reg):
        def _factory():
            runner = IngestionRunner(
                registry=reg,
                activity_ledger=ledger,
                state_db=state_db,
                plaud_collector=PlaudCollector(_EmptyTransport()),
                desk_collector=CallHistoryCollector(_EmptyTransport(), b"\x0a" * 32),
                notifier=_NoopNotifier(),
            )
            return runner, state_db

        return _factory

    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader", _fake_build_registry_and_reader
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_ingestion_runner_factory", _fake_ingestion_factory
    )

    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED

    conn = sqlite3.connect(db_path)
    try:
        confirmations = conn.execute("SELECT COUNT(*) FROM ingestion_confirmations").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
    finally:
        conn.close()
    assert confirmations == 0
    assert runs == 0
    assert state_db.has_unaccepted_run() is None


def test_desk_mode_replay_is_idempotent_zero_duplicate_admits_or_notes(
    tmp_path: Path, monkeypatch
) -> None:
    """Running the desk automated mode twice over the same underlying Desk
    row must not double-admit or double-write a note -- the same
    ActivityLedger/note_mirror idempotency the interactive path relies on
    protects the automated path too, since both go through the identical
    IngestionRunner seam."""
    data_dir = tmp_path / "plugin-data" / "team_duncan_contacts"
    data_dir.mkdir(parents=True)
    state_db = IngestionStateDb(data_dir / "ingestion_state.db")
    clock = _Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    registry = ContactRegistry(
        tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock
    )
    ledger = ActivityLedger(tmp_path / "activity.db", registry)

    contact_id = "c-1"
    reader = FakeGhlReader([{
        "id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Cory", "lastName": "Vasquez", "phone": CANARY_PHONE,
    }])
    prep = registry.prepare_activation(contact_id, reader)
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"
    clock.advance(hours=1)

    zdate = utc_to_apple_epoch(clock.now)
    desk_row = {"ZDATE": zdate, "ZADDRESS": CANARY_PHONE, "ZDURATION": 120, "ZORIGINATED": 0, "ZANSWERED": 1}
    transport = _DeskRowTransport([desk_row])
    ghl_client = _FakeNoteGhlClient()
    note_mirror = NoteMirror(ghl_client, state_db)

    def _fake_build_registry_and_reader(hermes_home):
        return registry, object(), "loc-1"

    def _fake_ingestion_factory(hermes_home, reg):
        def _factory():
            runner = IngestionRunner(
                registry=reg,
                activity_ledger=ledger,
                state_db=state_db,
                plaud_collector=PlaudCollector(_EmptyTransport()),
                desk_collector=CallHistoryCollector(transport, IDENTITY_KEY),
                notifier=_NoopNotifier(),
                note_mirror=note_mirror,
                clock=clock,
            )
            return runner, state_db

        return _factory

    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader", _fake_build_registry_and_reader
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_ingestion_runner_factory", _fake_ingestion_factory
    )

    first_exit = automation_runner.run(["desk"], hermes_home=tmp_path)
    second_exit = automation_runner.run(["desk"], hermes_home=tmp_path)

    assert first_exit == automation_runner.EXIT_COMPLETED
    assert second_exit == automation_runner.EXIT_COMPLETED
    assert ghl_client.create_calls == 1


def test_desk_failure_never_touches_plaud_heartbeat_and_vice_versa(
    tmp_path: Path, fake_registry_wiring
) -> None:
    """Source-local isolation: a Desk-mode failure must leave the Plaud
    heartbeat marker untouched, and a Plaud-mode failure must leave the
    Desk marker untouched."""
    fake_registry_wiring["runner"] = _FakeRunner(raises=RuntimeError("desk boom"))
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    store = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts")
    assert store.read("desk")["last_outcome"] == "failed"
    assert store.read("plaud_reconcile") is None
    assert store.read("plaud_webhook") is None

    fake_registry_wiring["runner"] = _FakeRunner(raises=RuntimeError("plaud boom"))
    exit_code = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED

    # The prior Desk failure record is untouched by the Plaud failure.
    desk_state_after = store.read("desk")
    assert desk_state_after["last_outcome"] == "failed"
    assert store.read("plaud_reconcile")["last_outcome"] == "failed"


# ---------------------------------------------------------------------------
# OPS-114: standalone Plaud automation must close its owned transport in a
# finally, on both a clean run and one that raises.
# ---------------------------------------------------------------------------


def test_plaud_reconcile_closes_runner_on_success(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(_FakeSummary({"matched": 0, "errors": 0}))
    exit_code = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].close_calls == 1


def test_plaud_reconcile_closes_runner_on_failure(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(raises=RuntimeError("plaud boom"))
    exit_code = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_FAILED
    assert fake_registry_wiring["runner"].close_calls == 1


def test_plaud_webhook_closes_runner_on_success(tmp_path: Path, fake_registry_wiring) -> None:
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-close-ok"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].close_calls == 1


def test_plaud_webhook_closes_runner_on_failure(tmp_path: Path, fake_registry_wiring) -> None:
    fake_registry_wiring["runner"] = _FakeRunner(raises=RuntimeError("webhook boom"))
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-close-fail"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_FAILED
    assert fake_registry_wiring["runner"].close_calls == 1


def test_desk_mode_never_calls_close(tmp_path: Path, fake_registry_wiring) -> None:
    """Desk automation is unchanged by this fix -- it never closes a
    transport the way the Plaud modes now do."""
    exit_code = automation_runner.run(["desk"], hermes_home=tmp_path)
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert fake_registry_wiring["runner"].close_calls == 0
