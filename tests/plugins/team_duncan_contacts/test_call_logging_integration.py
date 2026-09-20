"""Canary sweep: run a mixed ingestion batch (Desk zero-match, Plaud
zero-match, known pre-activation) and assert canary sensitive values never
appear in captured state, notification output, run summaries, queue output,
or exceptions -- across the whole plugin, not just one module at a time.

Real ContactRegistry/ActivityLedger/IngestionStateDb against temp
directories. No live Desk/Plaud/Telegram/email/GHL access anywhere.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector, utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_runner import IngestionRunner
from plugins.team_duncan_contacts.ingestion_state_db import (
    IngestionStateDb, list_pending_call_reviews,
)
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

# Canary values: must never appear, in any form, outside the registry's own
# HMAC-index boundary and the transient in-memory collector records.
CANARY_PHONE = "+15551239999"
CANARY_PHONE_DIGITS = "5551239999"
CANARY_EMAIL = "canary.borrower@example.com"
CANARY_EMAIL_LOCAL = "canary.borrower"
CANARY_UNREGISTERED_PHONE = "+15557778888"
CANARY_UNREGISTERED_DIGITS = "5557778888"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class FakePlaudTransport:
    def __init__(self, records):
        self._records = records

    def get_deployment_boundary(self):
        return "boundary-0"

    def fetch_records_since(self, checkpoint):
        return list(self._records)

    def fetch_record_by_identity(self, recording_id):
        for r in self._records:
            if r["recording_id"] == recording_id:
                return r
        return None


class FakeDeskTransport:
    def __init__(self, rows):
        self._rows = rows

    def run_routine_scan(self, now):
        return list(self._rows)

    def run_replay_lookup(self, **kwargs):
        return list(self._rows)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    def send(self, payload) -> bool:
        self.sent.append(payload)
        return True


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc))


def test_canary_values_absent_from_every_captured_surface(tmp_path: Path, clock: _Clock) -> None:
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock)
    activity_ledger = ActivityLedger(tmp_path / "activity.db", registry)
    state_db = IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)

    # Contact with the canary phone/email, activated well in the past so a
    # pre-cutoff call is straightforward to construct.
    reader = FakeGhlReader([{
        "id": "c-canary", "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Canary", "lastName": "Borrower",
        "phone": CANARY_PHONE, "email": CANARY_EMAIL,
    }])
    prep = registry.prepare_activation("c-canary", reader)
    registry.confirm_activation(prep.token)

    before_cutoff = clock.now - timedelta(days=2)
    desk_rows = [
        {"ZDATE": utc_to_apple_epoch(before_cutoff), "ZADDRESS": CANARY_PHONE,
         "ZDURATION": 45, "ZORIGINATED": 1, "ZANSWERED": 1},  # deny_pre_activation
        {"ZDATE": utc_to_apple_epoch(clock.now), "ZADDRESS": CANARY_UNREGISTERED_PHONE,
         "ZDURATION": 20, "ZORIGINATED": 0, "ZANSWERED": 0},  # desk zero_match
    ]
    plaud_records = [{
        "recording_id": "rec-canary-1", "start_time": clock.now.isoformat(),
        "duration_s": 15, "caller_handle": CANARY_UNREGISTERED_PHONE,
        "transcript_available": False, "summary_available": False,
    }]

    notifier = RecordingNotifier()
    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=PlaudCollector(FakePlaudTransport(plaud_records)),
        desk_collector=CallHistoryCollector(FakeDeskTransport(desk_rows), b"\x0c" * 32),
        notifier=notifier, clock=clock,
    )
    summary = runner.run()

    surfaces = []

    # 1. ingestion_state.db raw content.
    conn = sqlite3.connect(state_db._db_path)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        surfaces.append(f"{table}: {rows}")
    conn.close()

    # 2. activity.db raw content.
    conn = sqlite3.connect(tmp_path / "activity.db")
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        surfaces.append(f"{table}: {rows}")
    conn.close()

    # 3. Every notification payload actually sent.
    surfaces.append(json.dumps(notifier.sent, default=str))

    # 4. Run summary.
    surfaces.append(json.dumps(summary.to_dict(), default=str))

    # 5. Pending Call Reviews queue output.
    surfaces.append(json.dumps(list_pending_call_reviews(state_db, limit=50), default=str))

    # 6. registry.json itself must contain no *raw* canary value (masked
    #    forms and HMACs are expected and fine).
    surfaces.append((tmp_path / "registry" / "plugin-data" / "team_duncan_contacts" / "registry.json").read_text())

    blob = "\n".join(surfaces)
    assert CANARY_PHONE not in blob
    assert CANARY_PHONE_DIGITS not in blob
    assert CANARY_EMAIL not in blob
    assert CANARY_EMAIL_LOCAL not in blob
    assert CANARY_UNREGISTERED_PHONE not in blob
    assert CANARY_UNREGISTERED_DIGITS not in blob

    # Sanity: the scenario actually exercised something on every surface
    # checked (a vacuous sweep over empty data would prove nothing).
    assert summary.pending_review >= 2
    assert len(notifier.sent) >= 1


def test_canary_absent_from_exception_paths(tmp_path: Path, clock: _Clock) -> None:
    """A malformed record must not leak the canary handle into the error
    path (log call args are asserted separately in code review; here we
    assert the *returned* summary and raised state carry nothing raw)."""
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock)
    activity_ledger = ActivityLedger(tmp_path / "activity.db", registry)
    state_db = IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)

    reader = FakeGhlReader([{
        "id": "c-baseline", "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Base", "lastName": "Line", "phone": "+15550000000",
    }])
    prep = registry.prepare_activation("c-baseline", reader)
    registry.confirm_activation(prep.token)

    class _BrokenTransport:
        def get_deployment_boundary(self):
            return "boundary-0"

        def fetch_records_since(self, checkpoint):
            # Missing recording_id -> normalize_record raises, deliberately
            # carrying no canary-shaped text in its message.
            return [{
                "start_time": clock.now.isoformat(), "duration_s": 1,
                "caller_handle": CANARY_PHONE,
            }]

        def fetch_record_by_identity(self, recording_id):
            return None

    runner = IngestionRunner(
        registry=registry, activity_ledger=activity_ledger, state_db=state_db,
        plaud_collector=PlaudCollector(_BrokenTransport()),
        desk_collector=CallHistoryCollector(FakeDeskTransport([]), b"\x0d" * 32),
        notifier=RecordingNotifier(), clock=clock,
    )
    summary = runner.run()
    blob = json.dumps(summary.to_dict(), default=str)
    assert CANARY_PHONE not in blob
    assert CANARY_PHONE_DIGITS not in blob
    assert summary.errors >= 1
