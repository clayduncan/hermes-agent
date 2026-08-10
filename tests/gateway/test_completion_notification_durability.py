"""Regression tests for durable, exactly-once background completion delivery.

Covers the guarantees the durable completion store must NOT break on its way
to fixing the lost-notification bug:

* ``async_delegation`` events ride the SAME ``completion_queue`` and own their
  own durable table — the new store must never see, delay, or replay one.
* ``completion_queue.qsize()`` is read directly by ``gateway/readiness.py`` and
  ``gateway/platforms/api_server.py`` for health reporting, so it must keep
  meaning "genuinely pending work".
* A replayed completion needs a consumer on the gateway: its original
  per-process watcher died with the previous process.

Store-level exactly-once lives in tests/tools/test_process_completion_store.py.
"""

import asyncio
import queue
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from gateway.run import (
    GatewayRunner,
    _drain_gateway_watch_events,
    _format_gateway_process_notification,
)
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
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _simulate_restart():
    conn = sqlite3.connect(store._db_path())
    try:
        with conn:
            conn.execute(
                "UPDATE process_completion_events SET owner_pid=?, owner_started_at=NULL",
                (_reaped_pid(),),
            )
    except sqlite3.OperationalError:
        pass  # nothing was ever persisted — a restart changes nothing
    finally:
        conn.close()
    store._restored_this_process.clear()


def _async_delegation_evt(delegation_id="del_1"):
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "telegram:dm:123",
        "parent_session_id": "sess-parent",
        "task": "summarize the logs",
        "result": "done",
    }


def _all_completion_rows():
    conn = sqlite3.connect(store._db_path())
    try:
        return conn.execute(
            "SELECT session_id FROM process_completion_events"
        ).fetchall()
    except sqlite3.OperationalError:
        # No table at all is the strongest form of "never touched": a queue
        # carrying only delegation events must not even create the schema.
        return []
    finally:
        conn.close()


def _finished_session(sid="proc_dur", notify=True):
    return ProcessSession(
        id=sid,
        command="make build",
        session_key="agent:main:telegram:dm:99",
        started_at=time.time() - 5,
        notify_on_complete=notify,
    )


def _exit_session(registry, session, exit_code=0):
    registry._running[session.id] = session
    session.exited = True
    session.exit_code = exit_code
    registry._move_to_finished(session)


# ---------------------------------------------------------------------------
# Constraint 1 — async_delegation is never intercepted
# ---------------------------------------------------------------------------

class TestAsyncDelegationIsolation:
    def test_delegation_events_are_requeued_untouched(self):
        """The requeue-and-skip behavior must stay byte-identical: the
        delegation watcher, not this drain, owns these events."""
        q = queue.Queue()
        evt = _async_delegation_evt()
        q.put(evt)

        assert _drain_gateway_watch_events(q) == []
        assert q.qsize() == 1
        requeued = q.get_nowait()
        assert requeued is evt
        assert requeued == _async_delegation_evt()  # not mutated, not stamped

    def test_delegation_events_never_reach_the_completion_store(self):
        q = queue.Queue()
        q.put(_async_delegation_evt())

        _drain_gateway_watch_events(q)

        assert _all_completion_rows() == []
        assert store.get_completion_record("del_1") is None

    def test_replay_never_picks_up_a_delegation_event(self):
        """Even with a delegation event sitting on the queue at boot, the
        process-completion replay has nothing of its own to restore."""
        registry = ProcessRegistry()
        registry.completion_queue.put(_async_delegation_evt())
        _simulate_restart()

        assert registry.restore_pending_completions() == 0
        assert registry.completion_queue.qsize() == 1
        assert registry.completion_queue.get_nowait()["type"] == "async_delegation"

    def test_a_delegation_event_drained_by_the_registry_is_not_acked(self):
        """``mark_completion_delivered`` is scoped to process completions; the
        delegation row is acknowledged by async_delegation's own code."""
        registry = ProcessRegistry()
        registry.completion_queue.put(_async_delegation_evt())

        registry.drain_notifications()

        assert _all_completion_rows() == []

    def test_the_two_stores_do_not_share_a_lock(self):
        """A burst of exiting processes must never serialize behind — or ahead
        of — delegate_task's delivery-claim I/O."""
        import tools.async_delegation as async_delegation

        assert store._DB_LOCK is not async_delegation._DB_LOCK
        assert store._db_path() != async_delegation._db_path()


# ---------------------------------------------------------------------------
# Constraint 2 — queue-depth semantics for readiness/health checks
# ---------------------------------------------------------------------------

class TestQueueDepthSemantics:
    def test_qsize_after_a_normal_enqueue_deliver_cycle(self):
        """Persisting BEFORE the put must not change what the healthcheck
        sees: one pending completion in, zero after it is consumed."""
        registry = ProcessRegistry()
        assert registry.completion_queue.qsize() == 0

        _exit_session(registry, _finished_session())
        assert registry.completion_queue.qsize() == 1  # genuinely pending

        assert len(registry.drain_notifications()) == 1
        assert registry.completion_queue.qsize() == 0  # nothing left owed

    def test_a_delivered_event_is_not_double_counted_after_a_restart(self):
        """The delivered row must not re-enter the queue and over-report a
        backlog that does not exist."""
        dying = ProcessRegistry()
        _exit_session(dying, _finished_session())
        dying.drain_notifications()  # consumed + acked

        _simulate_restart()

        reborn = ProcessRegistry()
        reborn.restore_pending_completions()
        assert reborn.completion_queue.qsize() == 0

    def test_a_replayed_event_is_counted_as_pending_work(self):
        """Under-reporting would hide a real backlog: the user is still owed
        this notification, so the depth must show it."""
        dying = ProcessRegistry()
        _exit_session(dying, _finished_session())

        _simulate_restart()

        reborn = ProcessRegistry()
        reborn.restore_pending_completions()
        assert reborn.completion_queue.qsize() == 1

    def test_readiness_reports_the_depth_it_is_handed(self):
        """Pin the contract the healthcheck actually reads."""
        from gateway.readiness import collect_runtime_readiness

        registry = ProcessRegistry()
        _exit_session(registry, _finished_session())

        report = collect_runtime_readiness(
            configured_model="claude-opus-4-8",
            runtime_status={"state": "running"},
            process_completion_queue_depth=registry.completion_queue.qsize(),
        )
        assert report["checks"]["background_queues"]["process_completions"] == 1


# ---------------------------------------------------------------------------
# Gateway-side delivery of replayed completions
# ---------------------------------------------------------------------------

class TestRestoredCompletionDelivery:
    def test_fresh_completions_are_still_dropped_by_the_post_turn_drain(self):
        """Unchanged: a fresh completion belongs to its per-process watcher."""
        q = queue.Queue()
        q.put({"type": "completion", "session_id": "proc_fresh", "command": "x"})

        assert _drain_gateway_watch_events(q) == []
        assert q.empty()

    def test_restored_completions_are_requeued_for_their_watcher(self):
        """Dropping one here would silently eat the very notification the
        replay exists to deliver."""
        q = queue.Queue()
        evt = {
            "type": "completion", "session_id": "proc_r",
            "command": "x", "restored": True,
        }
        q.put(evt)

        assert _drain_gateway_watch_events(q) == []
        assert q.qsize() == 1
        assert q.get_nowait() is evt

    def test_watch_events_still_drain(self):
        q = queue.Queue()
        q.put({"type": "watch_match", "session_id": "proc_w",
               "command": "tail -f log", "pattern": "ERROR", "output": "ERROR: x"})

        drained = _drain_gateway_watch_events(q)

        assert len(drained) == 1
        assert q.empty()

    def test_formatter_renders_restored_completions_only(self):
        base = {
            "type": "completion", "session_id": "proc_r", "command": "make build",
            "exit_code": 0, "output": "ok", "started_at": time.time(),
        }
        # Fresh: owned by _run_process_watcher, which formats from live state.
        assert _format_gateway_process_notification(dict(base)) is None

        text = _format_gateway_process_notification({**base, "restored": True})
        assert text
        assert "proc_r" in text

    def _stub_runner(self, registry, deliver_result):
        calls = []

        async def _deliver(synth_text, evt):
            calls.append((synth_text, evt))
            return deliver_result

        runner = SimpleNamespace(
            _running=True,
            calls=calls,
            _deliver_completion_notification=_deliver,
        )
        runner._enrich_async_delegation_routing = (
            GatewayRunner._enrich_async_delegation_routing.__get__(runner, SimpleNamespace)
        )
        return runner

    async def _run_watcher_once(self, runner):
        """Drive one pass of the watcher, then let it stop."""
        async def _stopper():
            await asyncio.sleep(0.05)
            runner._running = False

        task = asyncio.ensure_future(
            GatewayRunner._restored_completion_watcher(runner, interval=0.01)
        )
        await asyncio.gather(task, _stopper())

    @pytest.mark.asyncio
    async def test_watcher_delivers_a_replayed_completion_and_acks_it(self, monkeypatch):
        dying = ProcessRegistry()
        _exit_session(dying, _finished_session())
        _simulate_restart()

        reborn = ProcessRegistry()
        assert reborn.restore_pending_completions() == 1
        monkeypatch.setattr("tools.process_registry.process_registry", reborn)
        monkeypatch.setattr(asyncio, "sleep", _no_wait(asyncio.sleep))

        runner = self._stub_runner(reborn, True)
        await self._run_watcher_once(runner)

        assert len(runner.calls) == 1
        synth_text, evt = runner.calls[0]
        assert "proc_dur" in synth_text
        assert evt["restored"] is True
        # Routing derived from the persisted session_key, since the payload
        # from the dead process carries no platform/chat_id.
        assert evt["platform"] == "telegram"
        assert evt["chat_id"] == "99"
        assert reborn.completion_queue.empty()
        assert store.get_completion_record("proc_dur")["delivery_state"] == "delivered"

    @pytest.mark.asyncio
    async def test_failed_injection_leaves_the_event_pending_and_queued(self, monkeypatch):
        dying = ProcessRegistry()
        _exit_session(dying, _finished_session())
        _simulate_restart()

        reborn = ProcessRegistry()
        reborn.restore_pending_completions()
        monkeypatch.setattr("tools.process_registry.process_registry", reborn)
        monkeypatch.setattr(asyncio, "sleep", _no_wait(asyncio.sleep))

        runner = self._stub_runner(reborn, False)
        await self._run_watcher_once(runner)

        assert runner.calls  # it tried
        assert reborn.completion_queue.qsize() == 1  # requeued for retry
        assert store.get_completion_record("proc_dur")["delivery_state"] == "pending"

    @pytest.mark.asyncio
    async def test_watcher_gives_up_after_repeated_failures(self, monkeypatch):
        """A permanently failing adapter must not be retried for the lifetime
        of the gateway, and the abandoned event must not linger on the queue
        inflating the healthcheck's depth. The row stays pending, so the next
        boot tries again (bounded by the store's own replay cap)."""
        dying = ProcessRegistry()
        _exit_session(dying, _finished_session())
        _simulate_restart()

        reborn = ProcessRegistry()
        reborn.restore_pending_completions()
        monkeypatch.setattr("tools.process_registry.process_registry", reborn)
        monkeypatch.setattr(asyncio, "sleep", _no_wait(asyncio.sleep))

        runner = self._stub_runner(reborn, False)
        await asyncio.wait_for(
            GatewayRunner._restored_completion_watcher(runner, interval=0),
            timeout=5.0,
        )

        assert len(runner.calls) == 10  # MAX_DELIVERY_PASSES, then it stops
        assert reborn.completion_queue.empty()
        assert store.get_completion_record("proc_dur")["delivery_state"] == "pending"

    @pytest.mark.asyncio
    async def test_watcher_exits_immediately_when_nothing_was_replayed(self, monkeypatch):
        registry = ProcessRegistry()
        registry.completion_queue.put(_async_delegation_evt())
        monkeypatch.setattr("tools.process_registry.process_registry", registry)

        runner = self._stub_runner(registry, True)
        # No sleep patching: if the watcher did not return immediately this
        # would hang on its 3-second platform-connect wait.
        await asyncio.wait_for(
            GatewayRunner._restored_completion_watcher(runner, interval=0.01),
            timeout=1.0,
        )

        assert runner.calls == []
        assert registry.completion_queue.qsize() == 1  # delegation event untouched


def _no_wait(real_sleep):
    """Collapse the watcher's platform-connect delay without busy-looping."""
    async def _sleep(delay, *a, **kw):
        return await real_sleep(0 if delay >= 1 else delay, *a, **kw)
    return _sleep
