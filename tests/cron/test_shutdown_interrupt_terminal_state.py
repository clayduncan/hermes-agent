"""Regression guard: adding durable terminal-state persistence to the process
kill path must not perturb ``cron/scheduler.py``'s interrupted-marking.

``gateway/run.py``'s ``_kill_tool_subprocesses(phase)`` runs, in this exact
order:

    1. ``process_registry.kill_all()``
    2. ``cron.scheduler.mark_running_jobs_interrupted(...)``
    3. ``async_delegation.interrupt_all(...)``
    4. terminal-env / browser cleanup

Step 2 exists because ``kill_all()`` has no per-job targeting: any cron job
dispatched at that instant just had its tool subprocess killed out from under
it, and its agent thread may still produce a plausible-looking final response
from the truncated output. Marking the run interrupted is what stops the
scheduler ever reporting that as a success (#60432).

The terminal-state write added to ``_move_to_finished`` executes inside step 1,
once per session actually killed. These tests pin that:
  - step 1 still kills and still returns its count,
  - step 2 still marks every in-flight job interrupted, with success=False,
  - ``run_one_job`` still refuses to write a success for a marked job,
  - and none of that changes when the persistence write FAILS — teardown is
    best-effort and must not short-circuit.

``cron/scheduler.py`` itself is read-only here; nothing in it was modified.
"""

import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()


@pytest.fixture(autouse=True)
def _reset_terminal_store():
    from tools.process_terminal_store import _reset_for_tests

    _reset_for_tests()
    yield
    _reset_for_tests()


def _tracked_session(registry, sid):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    session = ProcessSession(
        id=sid,
        command="sleep 60",
        task_id="cron-task",
        pid=proc.pid,
        process=proc,
        started_at=time.time(),
        host_start_time=ProcessRegistry._safe_host_start_time(proc.pid),
    )
    registry._running[sid] = session
    return session, proc


def _kill_then_mark(registry, phase="final-cleanup"):
    """Replay the two steps ``gateway/run.py``'s ``_kill_tool_subprocesses``
    performs, in the same order, against a real registry."""
    import cron.scheduler as sched

    killed = registry.kill_all()
    interrupted = sched.mark_running_jobs_interrupted(
        f"Gateway shutdown ({phase}) killed the job's tool "
        "subprocess before the run finished."
    )
    return killed, interrupted


class TestKillThenMarkSequence:
    def test_in_flight_job_is_marked_interrupted_after_a_real_kill(self):
        import cron.scheduler as sched

        registry = ProcessRegistry()
        sched._running_job_ids.add("job-1")
        _session, proc = _tracked_session(registry, "proc_cron_1")
        try:
            with patch("cron.scheduler.mark_job_run") as mock_mark:
                killed, interrupted = _kill_then_mark(registry)
        finally:
            proc.kill()
            proc.wait()

        assert killed == 1
        assert interrupted == ["job-1"]
        assert "job-1" in sched._interrupted_job_ids
        # An interrupted run is never "ok".
        assert mock_mark.call_args.args[1] is False
        assert "gateway shutdown" in mock_mark.call_args.args[2].lower()

    def test_terminal_state_of_the_killed_session_is_recoverable(self):
        """The cron job's tool subprocess outcome now survives the restart the
        job's own status write may not — that is the point of the change."""
        import cron.scheduler as sched

        registry = ProcessRegistry()
        sched._running_job_ids.add("job-1")
        session, proc = _tracked_session(registry, "proc_cron_2")
        try:
            with patch("cron.scheduler.mark_job_run"):
                _kill_then_mark(registry)
        finally:
            proc.kill()
            proc.wait()

        restarted = ProcessRegistry()
        polled = restarted.poll(session.id)
        assert polled["status"] == "exited"
        assert polled["exit_code"] == -15
        assert polled["completion_reason"] == "killed"

    def test_persistence_failure_does_not_stop_the_interrupt_marking(self):
        """If step 1's durable write blows up, step 2 must still run — a
        swallowed persistence error must never leave a cron job unmarked and
        therefore reportable as a success."""
        import cron.scheduler as sched

        registry = ProcessRegistry()
        sched._running_job_ids.add("job-1")
        _session, proc = _tracked_session(registry, "proc_cron_3")
        try:
            with patch(
                "tools.process_terminal_store.record_terminal_state",
                side_effect=OSError("disk full"),
            ), patch("cron.scheduler.mark_job_run") as mock_mark:
                killed, interrupted = _kill_then_mark(registry)
        finally:
            proc.kill()
            proc.wait()

        assert killed == 1
        assert interrupted == ["job-1"]
        assert mock_mark.call_args.args[1] is False

    def test_marking_is_a_no_op_when_no_cron_job_is_in_flight(self):
        """The per-turn ``kill_all()`` call site fires constantly during normal
        operation; it must not manufacture cron status writes."""
        registry = ProcessRegistry()
        _session, proc = _tracked_session(registry, "proc_cron_4")
        try:
            with patch("cron.scheduler.mark_job_run") as mock_mark:
                killed, interrupted = _kill_then_mark(registry)
        finally:
            proc.kill()
            proc.wait()

        assert killed == 1
        assert interrupted == []
        mock_mark.assert_not_called()


class TestRunOneJobStillHonoursTheFlag:
    """End of the chain: the flag set during shutdown must still beat the
    job thread's own late, plausible-looking success."""

    def test_success_write_is_suppressed_for_an_interrupted_job(self):
        import cron.scheduler as sched

        registry = ProcessRegistry()
        job = {"id": "job-1", "name": "test job", "prompt": "do work"}
        sched._running_job_ids.add(job["id"])
        _session, proc = _tracked_session(registry, "proc_cron_5")
        try:
            with patch("cron.scheduler.mark_job_run"):
                _kill_then_mark(registry)
        finally:
            proc.kill()
            proc.wait()

        assert job["id"] in sched._interrupted_job_ids

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value=None), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is True
        mock_mark.assert_not_called()
