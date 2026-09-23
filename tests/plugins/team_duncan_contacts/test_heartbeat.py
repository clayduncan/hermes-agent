"""Deterministic tests for the OPS-114 content-free heartbeat store."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.heartbeat import (
    MARKER_DESK,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED_LOCK,
    HeartbeatStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> HeartbeatStore:
    return HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts")


def test_read_before_any_record_is_none(store: HeartbeatStore) -> None:
    assert store.read(MARKER_DESK) is None


def test_completed_run_advances_all_three_timestamps(store: HeartbeatStore) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    state = store.record(
        MARKER_DESK, outcome=OUTCOME_COMPLETED, mode="desk",
        counts={"admitted": 2, "errors": 0}, now=now,
    )
    assert state["last_attempt_at"] == now.isoformat()
    assert state["last_completed_at"] == now.isoformat()
    assert state["last_success_at"] == now.isoformat()
    assert state["last_outcome"] == "completed"
    assert state["counts"] == {"admitted": 2, "errors": 0}
    assert state["active_owner_pid"] is None
    assert state["error_class"] is None


def test_skipped_lock_advances_only_attempt(store: HeartbeatStore) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    state = store.record(
        MARKER_DESK, outcome=OUTCOME_SKIPPED_LOCK, mode="desk", owner_pid=4242, now=now,
    )
    assert state["last_attempt_at"] == now.isoformat()
    assert state["last_completed_at"] is None
    assert state["last_success_at"] is None
    assert state["last_outcome"] == "skipped_lock"
    assert state["active_owner_pid"] == 4242


def test_failed_run_advances_attempt_and_completed_not_success(store: HeartbeatStore) -> None:
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    state = store.record(
        MARKER_DESK, outcome=OUTCOME_FAILED, mode="desk", error_class="RuntimeError", now=now,
    )
    assert state["last_attempt_at"] == now.isoformat()
    assert state["last_completed_at"] == now.isoformat()
    assert state["last_success_at"] is None
    assert state["last_outcome"] == "failed"
    assert state["error_class"] == "RuntimeError"


def test_last_success_at_persists_across_a_later_failure(store: HeartbeatStore) -> None:
    """A prior success's last_success_at must survive an intervening
    failure -- absence detection depends on the last *successful* run, not
    the last attempt."""
    t1 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 22, 10, 15, 0, tzinfo=timezone.utc)
    store.record(MARKER_DESK, outcome=OUTCOME_COMPLETED, mode="desk", now=t1)
    state = store.record(
        MARKER_DESK, outcome=OUTCOME_FAILED, mode="desk", error_class="ValueError", now=t2,
    )
    assert state["last_success_at"] == t1.isoformat()
    assert state["last_completed_at"] == t2.isoformat()
    assert state["last_attempt_at"] == t2.isoformat()


def test_repeated_lock_skips_never_advance_last_success_at(store: HeartbeatStore) -> None:
    t1 = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 22, 10, 15, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 22, 10, 30, 0, tzinfo=timezone.utc)
    store.record(MARKER_DESK, outcome=OUTCOME_COMPLETED, mode="desk", now=t1)
    store.record(MARKER_DESK, outcome=OUTCOME_SKIPPED_LOCK, mode="desk", now=t2, owner_pid=1)
    state = store.record(MARKER_DESK, outcome=OUTCOME_SKIPPED_LOCK, mode="desk", now=t3, owner_pid=1)
    assert state["last_success_at"] == t1.isoformat()
    assert state["last_attempt_at"] == t3.isoformat()


def test_unknown_outcome_raises(store: HeartbeatStore) -> None:
    with pytest.raises(ValueError):
        store.record(MARKER_DESK, outcome="bogus", mode="desk")


def test_write_is_atomic_and_content_free(store: HeartbeatStore, tmp_path: Path) -> None:
    store.record(
        MARKER_DESK, outcome=OUTCOME_COMPLETED, mode="desk",
        counts={"admitted": 1, "run_id": "abc123"},
    )
    path = tmp_path / "plugin-data" / "team_duncan_contacts" / "automation_heartbeats" / "desk.json"
    assert path.exists()
    raw = path.read_text()
    # No temp artifact left behind after an atomic os.replace.
    assert not list(path.parent.glob("*.tmp*"))
    data = json.loads(raw)
    assert set(data["counts"].keys()) == {"admitted", "run_id"}


def test_markers_are_independent(store: HeartbeatStore) -> None:
    from plugins.team_duncan_contacts.heartbeat import MARKER_PLAUD_RECONCILE

    store.record(MARKER_DESK, outcome=OUTCOME_COMPLETED, mode="desk")
    assert store.read(MARKER_PLAUD_RECONCILE) is None
