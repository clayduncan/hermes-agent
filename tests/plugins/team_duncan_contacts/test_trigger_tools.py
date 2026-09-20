"""Tests for the OPS-18 agent-facing tools: list_pending_call_reviews and
the prepare/confirm/accept call-log ingestion manual-run gate.

No live transport is ever constructed here; confirm_call_log_ingest is
exercised against a runner_factory backed entirely by fakes/no-op stand-ins.
No cron/background invocation can reach a source read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ingestion_runner import IngestionRunner
from plugins.team_duncan_contacts.collectors.call_history_collector import CallHistoryCollector
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.registry import ContactRegistry
from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.tools import (
    make_accept_call_log_ingest_run_handler,
    make_confirm_call_log_ingest_handler,
    make_list_pending_call_reviews_handler,
    make_prepare_call_log_ingest_handler,
)
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID


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


@pytest.fixture(autouse=True)
def _clear_cron_env(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)


@pytest.fixture()
def state_db(tmp_path: Path) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db")


@pytest.fixture()
def runner_factory(tmp_path: Path, state_db: IngestionStateDb):
    registry = ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID)
    ledger = ActivityLedger(tmp_path / "activity.db", registry)

    def _factory():
        runner = IngestionRunner(
            registry=registry, activity_ledger=ledger, state_db=state_db,
            plaud_collector=PlaudCollector(_EmptyTransport()),
            desk_collector=CallHistoryCollector(_EmptyTransport(), b"\x0a" * 32),
            notifier=_NoopNotifier(),
        )
        return runner, state_db

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
