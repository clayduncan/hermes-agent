"""Tests for the OPS-18 agent-facing tools: list_pending_call_reviews and
the prepare/confirm/accept call-log ingestion manual-run gate.

No live transport is ever constructed here; confirm_call_log_ingest is
exercised against a runner_factory backed entirely by fakes/no-op stand-ins.
No cron/background invocation can reach a source read.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_runner import IngestionRunner
from plugins.team_duncan_contacts.collectors.call_history_collector import CallHistoryCollector
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.collectors.plaud_transcript import TranscriptPage
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.plaud_note_writer import PlaudSummaryNoteWriter
from plugins.team_duncan_contacts.plaud_summary_runner import PlaudSummaryRunner
from plugins.team_duncan_contacts.registry import ContactRegistry
from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.tools import (
    make_accept_call_log_ingest_run_handler,
    make_confirm_call_log_ingest_handler,
    make_confirm_plaud_summary_run_handler,
    make_list_pending_call_reviews_handler,
    make_prepare_call_log_ingest_handler,
    make_prepare_plaud_summary_run_handler,
)
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID


class _EmptyTransport:
    """Fake source transport. Counts routine/replay invocations so tests can
    assert exactly how many times (if any) a source read was reached --
    never a real SSH, Plaud, or network call."""

    def __init__(self) -> None:
        self.routine_calls = 0
        self.replay_calls = 0

    def get_deployment_boundary(self):
        return "boundary-0"

    def fetch_records_since(self, checkpoint):
        return []

    def fetch_record_by_identity(self, recording_id):
        return None

    def run_routine_scan(self, now):
        self.routine_calls += 1
        return []

    def run_replay_lookup(self, **kwargs):
        self.replay_calls += 1
        return []


class _NoopNotifier:
    def send(self, payload):
        return False


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@pytest.fixture(autouse=True)
def _clear_cron_env(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture()
def state_db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


@pytest.fixture()
def runner_factory(tmp_path: Path, state_db: IngestionStateDb):
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID)
    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    desk_transport = _EmptyTransport()

    def _factory():
        runner = IngestionRunner(
            registry=registry, activity_ledger=ledger, state_db=state_db,
            plaud_collector=PlaudCollector(_EmptyTransport()),
            desk_collector=CallHistoryCollector(desk_transport, b"\x0a" * 32),
            notifier=_NoopNotifier(),
        )
        return runner, state_db

    _factory.desk_transport = desk_transport
    return _factory


# --- list_pending_call_reviews ------------------------------------------------

def test_list_pending_call_reviews_tool_sanitized(state_db) -> None:
    state_db.insert_pending_review(
        source="plaud", source_event_id="evt-secret", decision="review_required",
        match_outcome="zero_match", occurred_at=datetime.now(timezone.utc).isoformat(),
        duration_s=1, direction=None, answered=None, status="pending_review",
        display_name="Do Not Leak", masked_labels={"phone": "***-***-1234"},
    )
    handler = make_list_pending_call_reviews_handler(state_db)
    raw = handler({})
    result = json.loads(raw)
    assert result["pending_review_count"] == 1
    assert len(result["rows"]) == 1
    assert "evt-secret" not in raw
    assert "Do Not Leak" not in raw
    assert "1234" not in raw


def test_list_pending_call_reviews_tool_rejects_absurd_limit_safely(state_db) -> None:
    handler = make_list_pending_call_reviews_handler(state_db)
    raw = handler({"limit": 999999})
    result = json.loads(raw)
    assert isinstance(result["rows"], list)  # bounded internally, never errors out raw


# --- prepare_call_log_ingest ---------------------------------------------------

def test_prepare_rejects_cron_context(state_db, monkeypatch) -> None:
    from plugins.team_duncan_contacts.tools import make_prepare_call_log_ingest_handler

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    handler = make_prepare_call_log_ingest_handler(state_db)
    result = json.loads(handler({}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"


def test_prepare_issues_token_with_backlog_preview(state_db) -> None:
    handler = make_prepare_call_log_ingest_handler(state_db)
    result = json.loads(handler({}))
    assert result["status"] == "ready_for_confirmation"
    assert "token" in result
    assert result["desk_lookback_days"] == 7
    assert result["pending_review_backlog"] == 0


def test_prepare_defaults_to_both_sources_for_backward_compatibility(state_db) -> None:
    """Direct construction without enabled_sources (pre-existing behavior
    for every caller that predates the source-isolation contract) keeps
    reporting both sources."""
    handler = make_prepare_call_log_ingest_handler(state_db)
    result = json.loads(handler({}))
    assert result["sources"] == ["plaud", "desk_call"]


def test_prepare_reports_only_configured_enabled_sources(state_db) -> None:
    """Per Clay's source-isolation correction: when the caller configures
    Desk-only (as the production factory now does), prepare_call_log_ingest
    must truthfully report only desk_call -- never claim Plaud."""
    handler = make_prepare_call_log_ingest_handler(state_db, enabled_sources=("desk_call",))
    result = json.loads(handler({}))
    assert result["sources"] == ["desk_call"]
    assert "plaud" not in result["sources"]


def test_prepare_rejects_when_unaccepted_run_outstanding(state_db, runner_factory) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)

    prep = json.loads(prepare_handler({}))
    confirm = json.loads(confirm_handler({"token": prep["token"]}))
    assert confirm["status"] == "awaiting_acceptance"

    second_prep = json.loads(prepare_handler({}))
    assert second_prep["status"] == "rejected"
    assert second_prep["reason"] == "unaccepted_run_outstanding"
    assert second_prep["run_id"] == confirm["run_id"]


# --- confirm_call_log_ingest ---------------------------------------------------

def test_confirm_rejects_cron_context(state_db, runner_factory, monkeypatch) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    prep = json.loads(prepare_handler({}))

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"

    # Token is NOT consumed by a rejected cron attempt.
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    result2 = json.loads(confirm_handler({"token": prep["token"]}))
    assert result2["status"] == "awaiting_acceptance"


def test_confirm_token_single_use_and_expiring(state_db, runner_factory) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)

    prep = json.loads(prepare_handler({}))
    first = json.loads(confirm_handler({"token": prep["token"]}))
    assert first["status"] == "awaiting_acceptance"
    assert "pending_review_count" in first
    assert "oldest_pending_at" in first

    second = json.loads(confirm_handler({"token": prep["token"]}))
    assert second["status"] == "rejected"
    assert second["reason"] == "token_not_found_or_expired"


def test_confirm_rejects_garbage_token(state_db, runner_factory) -> None:
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)
    result = json.loads(confirm_handler({"token": "not-a-real-token"}))
    assert result["status"] == "rejected"
    assert result["reason"] == "token_not_found_or_expired"


def test_confirm_valid_token_invokes_desk_exactly_once(state_db, runner_factory) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)

    prep = json.loads(prepare_handler({}))
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "awaiting_acceptance"
    assert runner_factory.desk_transport.routine_calls == 1


def test_confirm_invalid_expired_reused_cron_invoke_desk_zero_times(
    state_db, runner_factory, clock, monkeypatch
) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)
    desk_transport = runner_factory.desk_transport

    # Invalid (garbage) token.
    result = json.loads(confirm_handler({"token": "not-a-real-token"}))
    assert result["status"] == "rejected"
    assert desk_transport.routine_calls == 0

    # Expired token.
    expired_prep = json.loads(prepare_handler({}))
    clock.advance(seconds=301)
    result = json.loads(confirm_handler({"token": expired_prep["token"]}))
    assert result["status"] == "rejected"
    assert desk_transport.routine_calls == 0

    # Cron/background context: rejected before the token is even consumed.
    prep = json.loads(prepare_handler({}))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"
    assert desk_transport.routine_calls == 0
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    # The still-valid token now succeeds, invoking Desk exactly once.
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "awaiting_acceptance"
    assert desk_transport.routine_calls == 1

    # Reused token: rejected, invoking Desk zero additional times.
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "rejected"
    assert result["reason"] == "token_not_found_or_expired"
    assert desk_transport.routine_calls == 1


# --- accept_call_log_ingest_run -------------------------------------------------

def test_accept_rejects_cron_context(state_db, runner_factory, monkeypatch) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)
    prep = json.loads(prepare_handler({}))
    confirm = json.loads(confirm_handler({"token": prep["token"]}))

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    accept_handler = make_accept_call_log_ingest_run_handler(state_db)
    result = json.loads(accept_handler({"run_id": confirm["run_id"]}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"


def test_accept_unblocks_next_prepare(state_db, runner_factory) -> None:
    prepare_handler = make_prepare_call_log_ingest_handler(state_db)
    confirm_handler = make_confirm_call_log_ingest_handler(runner_factory)
    accept_handler = make_accept_call_log_ingest_run_handler(state_db)

    prep = json.loads(prepare_handler({}))
    confirm = json.loads(confirm_handler({"token": prep["token"]}))

    blocked = json.loads(prepare_handler({}))
    assert blocked["status"] == "rejected"

    accept_result = json.loads(accept_handler({"run_id": confirm["run_id"]}))
    assert accept_result["status"] == "accepted"

    unblocked = json.loads(prepare_handler({}))
    assert unblocked["status"] == "ready_for_confirmation"


def test_accept_rejects_unknown_run_id(state_db) -> None:
    accept_handler = make_accept_call_log_ingest_run_handler(state_db)
    result = json.loads(accept_handler({"run_id": "not-a-real-run"}))
    assert result["status"] == "rejected"
    assert result["reason"] == "run_not_found_or_already_accepted"


# ---------------------------------------------------------------------------
# OPS-110: prepare_plaud_summary_run / confirm_plaud_summary_run
# ---------------------------------------------------------------------------


class _EmptyTranscriptTransport:
    def fetch_transcript_page(self, recording_id, cursor):
        return TranscriptPage(segments=[], next_cursor=None)


class _EmptyNoteGhlClient:
    def create_note(self, *args, **kwargs):
        raise AssertionError("must not be called when there are zero Plaud records")

    def update_note(self, *args, **kwargs):
        raise AssertionError("must not be called when there are zero Plaud records")

    def get_note(self, *args, **kwargs):
        return None


@pytest.fixture()
def plaud_runner_factory(tmp_path: Path, state_db: IngestionStateDb):
    registry = ContactRegistry(
        tmp_path / "registry-plaud", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID
    )
    ledger = ActivityLedger(tmp_path / "activity-plaud.db", registry)
    desk_transport = _EmptyTransport()

    def _factory():
        runner = PlaudSummaryRunner(
            registry=registry,
            activity_ledger=ledger,
            state_db=state_db,
            plaud_collector=PlaudCollector(_EmptyTransport()),
            transcript_transport=_EmptyTranscriptTransport(),
            desk_collector=CallHistoryCollector(desk_transport, b"\x0a" * 32),
            ghl_reader=FakeGhlReader([]),
            note_writer=PlaudSummaryNoteWriter(_EmptyNoteGhlClient(), state_db),
            hermes_home=tmp_path,
        )
        return runner, state_db

    _factory.desk_transport = desk_transport
    return _factory


def test_prepare_plaud_summary_run_rejects_cron_context(state_db, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    handler = make_prepare_plaud_summary_run_handler(state_db)
    result = json.loads(handler({}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"


def test_prepare_plaud_summary_run_issues_token(state_db) -> None:
    handler = make_prepare_plaud_summary_run_handler(state_db)
    result = json.loads(handler({}))
    assert result["status"] == "ready_for_confirmation"
    assert "token" in result
    assert "token_expires_at" in result


def test_confirm_plaud_summary_run_rejects_cron_context(
    state_db, plaud_runner_factory, monkeypatch
) -> None:
    prep = json.loads(make_prepare_plaud_summary_run_handler(state_db)({}))

    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    confirm_handler = make_confirm_plaud_summary_run_handler(plaud_runner_factory)
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "rejected"
    assert result["reason"] == "cron_context"

    # Token is NOT consumed by a rejected cron attempt.
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    result2 = json.loads(confirm_handler({"token": prep["token"]}))
    assert result2["status"] == "completed"


def test_confirm_plaud_summary_run_token_single_use_and_expiring(
    state_db, plaud_runner_factory, clock
) -> None:
    prepare_handler = make_prepare_plaud_summary_run_handler(state_db)
    confirm_handler = make_confirm_plaud_summary_run_handler(plaud_runner_factory)

    prep = json.loads(prepare_handler({}))
    first = json.loads(confirm_handler({"token": prep["token"]}))
    assert first["status"] == "completed"
    assert first["matched"] == 0
    assert first["notes_written"] == 0
    assert first["errors"] == 0

    second = json.loads(confirm_handler({"token": prep["token"]}))
    assert second["status"] == "rejected"
    assert second["reason"] == "token_not_found_or_expired"

    expired_prep = json.loads(prepare_handler({}))
    clock.advance(seconds=301)
    expired = json.loads(confirm_handler({"token": expired_prep["token"]}))
    assert expired["status"] == "rejected"
    assert expired["reason"] == "token_not_found_or_expired"


def test_confirm_plaud_summary_run_rejects_garbage_token(state_db, plaud_runner_factory) -> None:
    confirm_handler = make_confirm_plaud_summary_run_handler(plaud_runner_factory)
    result = json.loads(confirm_handler({"token": "not-a-real-token"}))
    assert result["status"] == "rejected"
    assert result["reason"] == "token_not_found_or_expired"


def test_confirm_plaud_summary_run_with_zero_records_never_touches_ghl(
    state_db, plaud_runner_factory
) -> None:
    prep = json.loads(make_prepare_plaud_summary_run_handler(state_db)({}))
    confirm_handler = make_confirm_plaud_summary_run_handler(plaud_runner_factory)
    result = json.loads(confirm_handler({"token": prep["token"]}))
    assert result["status"] == "completed"
    assert result["errors"] == 0
    assert result["notes_written"] == 0
