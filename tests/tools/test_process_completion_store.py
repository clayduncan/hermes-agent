"""Tests for tools/process_completion_store.py — durable, exactly-once
completion notifications for background process sessions.

The bug these pin: ``ProcessRegistry._move_to_finished()`` enqueues a
``type="completion"`` event onto the in-memory ``completion_queue`` and the
gateway turns it into the synthetic ``[IMPORTANT: ...]`` message the user sees.
If the process dies between those two moments — the shutdown sequence drops the
platform adapter ~10 ms before the kill, so it does — the event is
garbage-collected and the user never hears their job finished.

Exactly-once is the crux, so the assertions come in pairs: a pending event MUST
survive a restart and replay, and a delivered event MUST NOT.
"""

import queue
import sqlite3
import subprocess
import sys
import time

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession
import tools.process_completion_store as store
import tools.process_terminal_store as terminal_store


@pytest.fixture(autouse=True)
def _reset_store():
    store._reset_for_tests()
    terminal_store._reset_for_tests()
    yield
    store._reset_for_tests()
    terminal_store._reset_for_tests()


def _reaped_pid() -> int:
    """A PID that is guaranteed dead — how a restart is simulated."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _completion_evt(session_id="proc_1", **kw):
    evt = {
        "type": "completion",
        "session_id": session_id,
        "session_key": "telegram:dm:123",
        "command": "sleep 60",
        "exit_code": 0,
        "completion_reason": "exited",
        "termination_source": "",
        "output": "all done",
        "started_at": time.time() - 30,
    }
    evt.update(kw)
    return evt


def _simulate_restart(*session_ids):
    """Make the durable rows look like they were written by a dead process.

    A restart is exactly two things from the store's point of view: the owner
    process is gone (so its in-memory queue went with it) and this process has
    no memory of having replayed anything yet.
    """
    dead = _reaped_pid()
    conn = sqlite3.connect(store._db_path())
    try:
        with conn:
            if session_ids:
                for sid in session_ids:
                    conn.execute(
                        "UPDATE process_completion_events SET owner_pid=?, "
                        "owner_started_at=NULL WHERE session_id=?",
                        (dead, sid),
                    )
            else:
                conn.execute(
                    "UPDATE process_completion_events SET owner_pid=?, "
                    "owner_started_at=NULL",
                    (dead,),
                )
    finally:
        conn.close()
    store._restored_this_process.clear()


class TestRecordAndAck:
    def test_pending_roundtrip(self):
        assert store.record_pending_completion(_completion_evt()) is True

        record = store.get_completion_record("proc_1")
        assert record is not None
        assert record["delivery_state"] == "pending"
        assert record["delivered_at"] is None
        assert record["event"]["exit_code"] == 0
        assert record["event"]["command"] == "sleep 60"

    def test_event_without_session_id_is_rejected(self):
        assert store.record_pending_completion({"type": "completion"}) is False

    def test_marking_delivered_flips_state_once(self):
        store.record_pending_completion(_completion_evt())

        assert store.mark_completion_delivered("proc_1") is True
        # Idempotent: a second ack changes nothing and reports so.
        assert store.mark_completion_delivered("proc_1") is False

        record = store.get_completion_record("proc_1")
        assert record["delivery_state"] == "delivered"
        assert record["delivered_at"] is not None

    def test_a_delivered_row_is_never_resurrected_as_pending(self):
        """``INSERT OR IGNORE`` is load-bearing: a late duplicate producer must
        not push an already-delivered event back into the replay set."""
        store.record_pending_completion(_completion_evt())
        store.mark_completion_delivered("proc_1")

        assert store.record_pending_completion(_completion_evt()) is False
        assert store.get_completion_record("proc_1")["delivery_state"] == "delivered"

    def test_command_and_output_are_redacted_before_hitting_disk(self):
        store.record_pending_completion(
            _completion_evt(
                command="curl -H 'Authorization: Bearer sk-ant-api03-SECRETVALUE'",
                output="token=sk-ant-api03-SECRETVALUE",
            )
        )

        conn = sqlite3.connect(store._db_path())
        try:
            raw = conn.execute(
                "SELECT event_json FROM process_completion_events WHERE session_id='proc_1'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "SECRETVALUE" not in raw

    def test_write_never_raises_on_a_broken_database(self, monkeypatch):
        """Persistence is best-effort — process teardown must not break."""
        monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert store.record_pending_completion(_completion_evt()) is False
        assert store.mark_completion_delivered("proc_1") is False
        assert store.restore_pending_completions(queue.Queue()) == 0


class TestExactlyOnceReplay:
    def test_pending_event_survives_a_restart_and_replays_exactly_once(self):
        """(a) Not zero times — the original bug."""
        store.record_pending_completion(_completion_evt())
        _simulate_restart("proc_1")

        q = queue.Queue()
        assert store.restore_pending_completions(q) == 1
        assert q.qsize() == 1

        evt = q.get_nowait()
        assert evt["session_id"] == "proc_1"
        assert evt["type"] == "completion"
        assert evt["restored"] is True

    def test_delivered_event_is_never_replayed(self):
        """(b) Not twice — an acknowledged event stays acknowledged."""
        store.record_pending_completion(_completion_evt())
        store.mark_completion_delivered("proc_1")
        _simulate_restart("proc_1")

        q = queue.Queue()
        assert store.restore_pending_completions(q) == 0
        assert q.empty()

    def test_recovery_running_twice_does_not_duplicate(self):
        """(c) A retried/duplicated startup call is a no-op the second time.

        The row deliberately stays ``pending`` (nothing has been delivered
        yet), so the in-process guard — not the row state — has to carry this.
        """
        store.record_pending_completion(_completion_evt())
        _simulate_restart("proc_1")

        q = queue.Queue()
        assert store.restore_pending_completions(q) == 1
        assert store.restore_pending_completions(q) == 0
        assert q.qsize() == 1
        assert store.get_completion_record("proc_1")["delivery_state"] == "pending"

    def test_replayed_but_undelivered_event_replays_on_the_next_boot_too(self):
        """Still pending after a boot that failed to deliver → try again."""
        store.record_pending_completion(_completion_evt())
        _simulate_restart("proc_1")
        q1 = queue.Queue()
        store.restore_pending_completions(q1)

        _simulate_restart("proc_1")  # a second boot, still nothing acked
        q2 = queue.Queue()
        assert store.restore_pending_completions(q2) == 1

    def test_replay_is_capped_so_an_unroutable_event_cannot_loop_forever(self):
        store.record_pending_completion(_completion_evt())
        for _ in range(store.MAX_REPLAY_ATTEMPTS):
            _simulate_restart("proc_1")
            assert store.restore_pending_completions(queue.Queue()) == 1

        _simulate_restart("proc_1")
        assert store.restore_pending_completions(queue.Queue()) == 0
        assert store.get_completion_record("proc_1")["delivery_state"] == "dropped"

    def test_events_owned_by_a_live_process_are_not_stolen(self):
        """The owner is alive, so the event is still on ITS queue. Replaying it
        here is the duplicate [IMPORTANT: ...] message we exist to prevent."""
        store.record_pending_completion(_completion_evt())
        store._restored_this_process.clear()  # owner_pid stays this live process

        assert store.restore_pending_completions(queue.Queue()) == 0

    def test_restore_preserves_enqueue_order(self):
        for i in range(3):
            store.record_pending_completion(_completion_evt(session_id=f"proc_{i}"))
            time.sleep(0.001)
        _simulate_restart()

        q = queue.Queue()
        assert store.restore_pending_completions(q) == 3
        assert [q.get_nowait()["session_id"] for _ in range(3)] == [
            "proc_0", "proc_1", "proc_2",
        ]


class TestRetentionAndIsolation:
    def test_retention_matches_the_terminal_store(self):
        """Same shape of record, same policy — pinned so they cannot drift."""
        assert store.RETENTION_SECONDS == terminal_store.RETENTION_SECONDS
        assert store.MAX_RETAINED == terminal_store.MAX_RETAINED

    def test_rows_past_the_retention_window_are_pruned(self):
        store.record_pending_completion(_completion_evt(session_id="proc_ancient"))
        conn = sqlite3.connect(store._db_path())
        try:
            with conn:
                conn.execute(
                    "UPDATE process_completion_events SET created_at=? WHERE session_id=?",
                    (time.time() - store.RETENTION_SECONDS - 60, "proc_ancient"),
                )
        finally:
            conn.close()

        store.restore_pending_completions(queue.Queue())

        assert store.get_completion_record("proc_ancient") is None

    def test_row_cap_evicts_the_oldest(self, monkeypatch):
        monkeypatch.setattr(store, "MAX_RETAINED", 3)
        monkeypatch.setattr(store, "_PRUNE_EVERY", 1)
        for i in range(6):
            store.record_pending_completion(_completion_evt(session_id=f"proc_{i}"))
            time.sleep(0.001)

        conn = sqlite3.connect(store._db_path())
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM process_completion_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert total <= 3
        assert store.get_completion_record("proc_0") is None
        assert store.get_completion_record("proc_5") is not None

    def test_shares_the_processes_db_file_but_not_the_terminal_table(self):
        """Same file as Bug 1's store (no new artifact), own table and own lock
        — and emphatically NOT async_delegation's table, whose lock serializes
        every delegate_task delivery claim."""
        assert store._db_path() == terminal_store._db_path()
        assert store._DB_LOCK is not terminal_store._DB_LOCK

        store.record_pending_completion(_completion_evt())

        conn = sqlite3.connect(store._db_path())
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "process_completion_events" in tables
        assert "async_delegations" not in tables

    def test_terminal_store_rows_are_untouched(self):
        """Bug 1's behavior must be unchanged by Bug 2's new table."""
        session = ProcessSession(
            id="proc_1", command="sleep 60", started_at=time.time(),
            exited=True, exit_code=-15, completion_reason="killed",
        )
        terminal_store.record_terminal_state(session)
        store.record_pending_completion(_completion_evt())

        record = terminal_store.get_terminal_state("proc_1")
        assert record is not None
        assert record["completion_reason"] == "killed"


class TestRegistryIntegration:
    """The end-to-end path: _move_to_finished → durable row → replay."""

    def _finished_session(self, sid="proc_reg", notify=True):
        return ProcessSession(
            id=sid,
            command="make build",
            session_key="telegram:dm:99",
            started_at=time.time() - 5,
            notify_on_complete=notify,
        )

    def test_move_to_finished_persists_before_enqueueing(self):
        registry = ProcessRegistry()
        session = self._finished_session()
        registry._running[session.id] = session
        session.exited = True
        session.exit_code = 0

        registry._move_to_finished(session)

        assert registry.completion_queue.qsize() == 1
        record = store.get_completion_record("proc_reg")
        assert record is not None
        assert record["delivery_state"] == "pending"
        assert record["event"]["command"] == "make build"

    def test_no_durable_row_without_notify_on_complete(self):
        """Nothing was owed to the user, so nothing needs replaying."""
        registry = ProcessRegistry()
        session = self._finished_session(notify=False)
        registry._running[session.id] = session
        session.exited = True

        registry._move_to_finished(session)

        assert registry.completion_queue.empty()
        assert store.get_completion_record("proc_reg") is None

    def test_the_lost_notification_is_replayed_after_a_restart(self):
        """The bug, end to end: the process dies with the event still on the
        in-memory queue; the next boot's registry gets it back."""
        dying = ProcessRegistry()
        session = self._finished_session()
        dying._running[session.id] = session
        session.exited = True
        session.exit_code = 0
        dying._move_to_finished(session)
        del dying  # the queue dies with the process

        _simulate_restart("proc_reg")

        reborn = ProcessRegistry()
        assert reborn.completion_queue.empty()  # nothing replays until asked
        assert reborn.restore_pending_completions() == 1

        evt = reborn.completion_queue.get_nowait()
        assert evt["session_id"] == "proc_reg"
        assert evt["restored"] is True

    def test_a_delivered_completion_is_not_replayed_after_a_restart(self):
        dying = ProcessRegistry()
        session = self._finished_session()
        dying._running[session.id] = session
        session.exited = True
        dying._move_to_finished(session)
        dying.mark_completion_delivered("proc_reg")

        _simulate_restart("proc_reg")

        assert ProcessRegistry().restore_pending_completions() == 0

    def test_double_startup_replay_does_not_double_inject(self):
        dying = ProcessRegistry()
        session = self._finished_session()
        dying._running[session.id] = session
        session.exited = True
        dying._move_to_finished(session)

        _simulate_restart("proc_reg")

        reborn = ProcessRegistry()
        assert reborn.restore_pending_completions() == 1
        assert reborn.restore_pending_completions() == 0
        assert reborn.completion_queue.qsize() == 1

    def test_registry_construction_alone_never_replays(self):
        """A one-shot CLI invocation builds a registry too; it has no business
        adopting a dead gateway's pending user notifications."""
        store.record_pending_completion(_completion_evt())
        _simulate_restart("proc_1")

        assert ProcessRegistry().completion_queue.empty()

    def test_drain_notifications_acks_what_it_consumes(self):
        registry = ProcessRegistry()
        session = self._finished_session()
        registry._running[session.id] = session
        session.exited = True
        session.exit_code = 0
        registry._move_to_finished(session)

        drained = registry.drain_notifications()

        assert len(drained) == 1
        assert store.get_completion_record("proc_reg")["delivery_state"] == "delivered"

    def test_a_completion_the_agent_already_consumed_is_acked_not_replayed(self):
        """wait()/read_log() handed the agent the output inline — resolved, not
        lost, so a restart must not report the same job finished again."""
        registry = ProcessRegistry()
        session = self._finished_session()
        registry._running[session.id] = session
        session.exited = True
        registry._move_to_finished(session)
        registry._completion_consumed.add("proc_reg")

        assert registry.drain_notifications() == []
        assert store.get_completion_record("proc_reg")["delivery_state"] == "delivered"

    def test_persistence_failure_never_blocks_the_in_memory_enqueue(self, monkeypatch):
        """Durability is an addition, not a dependency: a broken DB must not
        cost us the notification in the process that is still alive."""
        monkeypatch.setattr(
            store, "record_pending_completion",
            lambda evt: (_ for _ in ()).throw(RuntimeError("disk on fire")),
        )
        registry = ProcessRegistry()
        session = self._finished_session()
        registry._running[session.id] = session
        session.exited = True

        registry._move_to_finished(session)

        assert registry.completion_queue.qsize() == 1
