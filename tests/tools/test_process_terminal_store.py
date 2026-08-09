"""Tests for tools/process_terminal_store.py — the durable terminal-state
store backing ``poll()`` correctness across a gateway restart.

The store deliberately mirrors ``tools/async_delegation.py``'s durable-record
pattern (WAL-with-fallback schema init, always-closing ``_transaction()``,
``owner_pid``/``owner_started_at`` PID-identity recovery, bounded retention)
while sharing none of its runtime state — its own DB file, its own lock, its
own retention policy. These tests pin that behavior.
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from tools.process_registry import ProcessSession
import tools.process_terminal_store as store


@pytest.fixture(autouse=True)
def _reset_store():
    store._reset_for_tests()
    yield
    store._reset_for_tests()


def _terminal_session(sid="proc_x", **kw):
    defaults = dict(
        id=sid,
        command="sleep 60",
        task_id="t1",
        session_key="sess-a",
        pid=4242,
        started_at=time.time() - 5,
        exited=True,
        exit_code=-15,
        completion_reason="killed",
        termination_source="kill_all",
    )
    defaults.update(kw)
    return ProcessSession(**defaults)


def _reaped_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestRecordAndFetch:
    def test_roundtrip(self):
        assert store.record_terminal_state(_terminal_session()) is True

        record = store.get_terminal_state("proc_x")
        assert record is not None
        assert record["exit_code"] == -15
        assert record["completion_reason"] == "killed"
        assert record["termination_source"] == "kill_all"
        assert record["session_key"] == "sess-a"
        assert record["owner_pid"] == os.getpid()
        assert record["exited_at"] >= record["started_at"]

    def test_unknown_id_is_none(self):
        assert store.get_terminal_state("proc_nope") is None
        assert store.get_terminal_state("") is None

    def test_running_session_is_not_recorded(self):
        """Only TERMINAL state belongs here — a still-running session is the
        JSON checkpoint's job, and recording it would let a restart report a
        live process as finished."""
        session = _terminal_session(sid="proc_running", exited=False, exit_code=None)
        assert store.record_terminal_state(session) is False
        assert store.get_terminal_state("proc_running") is None

    def test_re_recording_the_same_session_updates_in_place(self):
        store.record_terminal_state(_terminal_session(sid="proc_dup"))
        store.record_terminal_state(
            _terminal_session(sid="proc_dup", exit_code=0, completion_reason="exited")
        )

        conn = sqlite3.connect(store._db_path())
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM process_terminal_states WHERE session_id='proc_dup'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 1
        assert store.get_terminal_state("proc_dup")["exit_code"] == 0

    def test_command_is_redacted_before_hitting_disk(self):
        store.record_terminal_state(
            _terminal_session(
                sid="proc_secret",
                command="deploy --token=sk-ant-api03-TOPSECRETTOKENVALUE",
            )
        )
        stored = store.get_terminal_state("proc_secret")["command"]
        assert "TOPSECRETTOKENVALUE" not in stored

    def test_write_never_raises_on_a_broken_database(self, monkeypatch):
        monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(OSError("nope")))
        assert store.record_terminal_state(_terminal_session(sid="proc_boom")) is False


class TestOwnerLiveness:
    """PID-identity guard, same shape as
    ``async_delegation.recover_abandoned_delegations``: liveness alone is
    satisfiable by an unrelated process that inherited the number."""

    def test_self_is_live(self):
        from gateway.status import get_process_start_time

        assert store._owner_is_live(os.getpid(), get_process_start_time(os.getpid())) is True

    def test_reaped_pid_is_not_live(self):
        assert store._owner_is_live(_reaped_pid(), None) is False

    def test_missing_pid_is_not_live(self):
        assert store._owner_is_live(None, None) is False
        assert store._owner_is_live(0, None) is False

    def test_recycled_pid_is_not_live(self):
        """Alive PID, wrong start time → a different process wearing the same
        number. Must not be treated as our owner."""
        assert store._owner_is_live(os.getpid(), 1) is False


class TestRecoverTerminalStates:
    def _set_owner(self, session_id, pid):
        conn = sqlite3.connect(store._db_path())
        try:
            with conn:
                conn.execute(
                    "UPDATE process_terminal_states SET owner_pid=? WHERE session_id=?",
                    (pid, session_id),
                )
        finally:
            conn.close()

    def test_returns_records_whose_owner_died(self):
        store.record_terminal_state(_terminal_session(sid="proc_orphan"))
        self._set_owner("proc_orphan", _reaped_pid())

        recovered = store.recover_terminal_states()

        assert [r["session_id"] for r in recovered] == ["proc_orphan"]
        assert recovered[0]["completion_reason"] == "killed"

    def test_skips_records_owned_by_a_live_process(self):
        store.record_terminal_state(_terminal_session(sid="proc_mine"))

        assert store.recover_terminal_states() == []

    def test_empty_when_no_database_exists(self):
        assert store.recover_terminal_states() == []


class TestPruning:
    def test_recovery_prunes_records_past_the_retention_window(self):
        store.record_terminal_state(_terminal_session(sid="proc_ancient"))
        conn = sqlite3.connect(store._db_path())
        try:
            with conn:
                conn.execute(
                    "UPDATE process_terminal_states SET exited_at=? WHERE session_id=?",
                    (time.time() - store.RETENTION_SECONDS - 60, "proc_ancient"),
                )
        finally:
            conn.close()

        store.recover_terminal_states()

        assert store.get_terminal_state("proc_ancient") is None

    def test_row_cap_evicts_the_oldest(self, monkeypatch):
        monkeypatch.setattr(store, "MAX_RETAINED", 3)
        # Distinct, in-retention exit times so "oldest" is unambiguous.
        base = time.time() - 60
        for i in range(6):
            store.record_terminal_state(_terminal_session(sid=f"proc_{i}"))
            conn = sqlite3.connect(store._db_path())
            try:
                with conn:
                    conn.execute(
                        "UPDATE process_terminal_states SET exited_at=? WHERE session_id=?",
                        (base + i, f"proc_{i}"),
                    )
            finally:
                conn.close()

        store.recover_terminal_states()

        surviving = {
            sid for sid in (f"proc_{i}" for i in range(6))
            if store.get_terminal_state(sid) is not None
        }
        assert surviving == {"proc_3", "proc_4", "proc_5"}

    def test_write_path_prune_is_amortized_not_per_write(self, monkeypatch):
        """Pruning costs a COUNT plus two DELETEs; the write path is hot, so it
        must not run on every kill."""
        calls = []
        real_prune = store._prune_locked
        monkeypatch.setattr(
            store, "_prune_locked",
            lambda conn, now: (calls.append(now), real_prune(conn, now))[1],
        )

        for i in range(store._PRUNE_EVERY - 1):
            store.record_terminal_state(_terminal_session(sid=f"proc_a{i}"))
        assert calls == []

        store.record_terminal_state(_terminal_session(sid="proc_trigger"))
        assert len(calls) == 1


class TestIsolationFromAsyncDelegation:
    def test_uses_its_own_database_file_not_state_db(self):
        """``async_delegations`` rows must never share a file-level write lock
        with the process kill path — a burst of killed sessions must not be
        able to delay delegation delivery claims."""
        from hermes_cli.config import get_hermes_home

        assert store._db_path().name == "processes.db"
        assert store._db_path() != get_hermes_home() / "state.db"

    def test_does_not_create_the_async_delegations_table(self):
        from hermes_cli.config import get_hermes_home

        store.record_terminal_state(_terminal_session(sid="proc_iso"))

        conn = sqlite3.connect(store._db_path())
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        assert tables == {"process_terminal_states"}
        assert not (get_hermes_home() / "state.db").exists()


class TestRecoveryLimit:
    def test_limit_returns_the_most_recent_records_oldest_first(self):
        base = time.time() - 100
        for i in range(5):
            store.record_terminal_state(_terminal_session(sid=f"proc_{i}"))
            conn = sqlite3.connect(store._db_path())
            try:
                with conn:
                    conn.execute(
                        "UPDATE process_terminal_states "
                        "SET exited_at=?, owner_pid=? WHERE session_id=?",
                        (base + i, _reaped_pid(), f"proc_{i}"),
                    )
            finally:
                conn.close()

        recovered = store.recover_terminal_states(limit=2)

        assert [r["session_id"] for r in recovered] == ["proc_3", "proc_4"]
        # Older records are not rehydrated, but stay queryable by id.
        assert store.get_terminal_state("proc_0")["exit_code"] == -15
