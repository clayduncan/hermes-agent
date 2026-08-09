# Spec: Persist terminal state for killed background process sessions

## Root cause (confirmed via live incident 2026-08-09, diagnosis already done — do not re-diagnose)

`tools/process_registry.py`'s `kill_process()` sets `session.exited = True` before
calling `_move_to_finished(session)`. `_write_checkpoint()` (tools/process_registry.py
~line 2437-2439) only ever persists entries from `self._running` where `not s.exited`:

```python
for s in self._running.values():
    if not s.exited:
```

A session killed during gateway shutdown (`kill_all()`, called from
`gateway/run.py:13155` post-interrupt and `gateway/run.py:13253` final-cleanup) is
moved to `self._finished` and marked `exited=True` in the same call
(`kill_process()`, `_move_to_finished()`), so it is EXCLUDED from the very next
checkpoint write. If the gateway process then exits (SIGTERM from systemd, OOM,
crash — anything, not just a clean restart), all in-memory state (`_running`,
`_finished`) is gone. The next gateway boot's `recover_from_checkpoint()`
(`gateway/run.py:11165`) has zero record the session ever existed.
`process_registry.poll(session_id)` on that ID then returns
`{"status": "not_found", "error": "No process with ID <id>"}` instead of a real
terminal record showing `exit_code=-15`, `completion_reason="killed"`.

This silently erases the outcome of any background job killed during shutdown —
including real cron-adjacent verification runs, confirmed twice in one day
(2026-08-09) against `~/.hermes/scripts/tracey_backup_cron.sh` runs launched via
`terminal(background=true)` and via a `delegate_task` subagent's own terminal tool.

## What to build

Add durable, restart-surviving persistence of TERMINAL session state (exit_code,
completion_reason, exited timestamp, command, session id) so that a session killed
during shutdown is still recoverable via `poll()` after a subsequent gateway restart.

**Pattern to reuse (read this file fully before designing anything new):**
`tools/async_delegation.py` already solves an equivalent problem for delegated
subagent tasks:
- Durable SQLite-backed record with a `state`/`delivery_state` column
  (`async_delegations` table — read the schema and every `conn.execute` call in
  that file to understand the exact shape).
- `recover_abandoned_delegations()` (~line 293-341): on startup, classifies any
  record still marked `running`/`finalizing` whose owning PID (`owner_pid`,
  `owner_started_at`) no longer matches a live process (via `_pid_exists` +
  `get_process_start_time` epoch match, guards against PID reuse) as `state='unknown'`
  with a clear error message, rather than leaving it silently stuck.

**Your job:** decide, with both files open, whether `process_registry.py` should:
(a) import/reuse `async_delegation.py`'s SQLite persistence machinery directly, or
(b) build a `process_registry`-scoped equivalent (its own small durable table or
file-backed store, following the same PID-liveness-on-restart pattern but NOT
sharing `async_delegation.py`'s runtime state).

**Constraint that should weigh heavily on that decision:** `completion_queue` is
SHARED between `process_registry` and `async_delegation.py` — `async_delegation.py`
lines ~923 and ~1132 enqueue delegated-task (i.e. `delegate_task` subagent) results
onto that SAME queue object that `process_registry._move_to_finished()` also
enqueues onto. `delegate_task` notifications are the live mechanism the orchestrating
agent uses to receive subagent results — this exact plan/build conversation depends
on that path working correctly right now. **Do not let terminal-state persistence
for process_registry sessions couple to or change `async_delegation.py`'s own
queue-draining, delivery-state, or replay timing in any way that could alter
`delegate_task` completion behavior.** If you choose to reuse the SQLite machinery
(option a), it must be through a genuinely separate table/rows scoped to
`process_registry` sessions, never touching the `async_delegations` table's rows,
and must not add any new consumer/producer relationship on `completion_queue`
itself — this spec is about TERMINAL-STATE persistence (poll() correctness), not
completion-queue delivery (that is BUG 2, out of scope for this spec, do not touch
`completion_queue` draining/injection logic at all in this build).

Report which option you chose (a or b) and why, in your final summary.

## Hard constraints (non-negotiable, verify before touching any of this)

1. **The kill-before-disconnect ordering in `gateway/run.py` is UNTOUCHED.**
   `_kill_tool_subprocesses("post-interrupt")` at line 13155 runs BEFORE adapter
   teardown (`_bounded_adapter_teardown` loop at line 13191-13199) deliberately —
   this dodges a documented prior incident (#8202: under systemd, deferring
   subprocess cleanup too long risks systemd's own TimeoutStopSec escalating to
   SIGKILL on the whole cgroup before Hermes's own cleanup runs). Do not reorder,
   remove, or delay either `_kill_tool_subprocesses` call relative to adapter
   teardown. You may add a persistence WRITE call inside/near the existing kill
   path, but the existing sequencing and timing of kill vs. disconnect must be
   byte-for-byte the same as before this change.

2. **`run_agent.py:4283` calls `kill_all()` on a HOT path** — per-turn/per-task
   cleanup that fires constantly during normal operation, not just shutdown. Any
   persistence write added to the kill path executes on THIS call site too. It
   must be cheap: a single small synchronous SQLite write (or equivalent) is
   acceptable if `async_delegation.py`'s existing writes on comparable hot paths
   are already this cheap (check how often `_note_delivery_attempt` or similar
   fires and at what cost) — but do NOT add anything that blocks on network I/O,
   holds a lock across an await, or scales with the number of currently-running
   sessions. Benchmark or reason explicitly about this cost in your summary.

3. **`cron/scheduler.py:401`'s `mark_running_jobs_interrupted` depends on
   `kill_all()`'s existing timing/behavior** to correctly mark a cron job killed
   mid-shutdown as interrupted (not falsely reported as a successful run). This
   existing behavior must be verified to still hold after your change — add a
   regression test asserting it (see criteria.md).

4. **Do not touch `completion_queue` producer/consumer code, `_drain_gateway_watch_events`,
   or anything in `gateway/run.py` after line 13191 (adapter disconnect) related to
   notification delivery.** That is Bug 2's scope, a separate future spec. This
   build is TERMINAL-STATE persistence only (fixing what `poll()` returns after
   restart), not notification delivery.

## Scope fence — files this build is expected to touch

- `tools/process_registry.py` — the persistence write on kill, and the
  recovery/merge logic in `recover_from_checkpoint()` so a restart correctly
  surfaces the real terminal record via `poll()`.
- A new small SQLite table (own file or reusing `async_delegation.py`'s existing
  DB file with a clearly separate table name — your call per the reuse decision
  above) OR a new lightweight persistence module if you choose option (b).
- New/updated tests under `tests/tools/test_process_registry.py` and a new
  regression test file if warranted (e.g. asserting `cron/scheduler.py`'s
  interrupted-marking behavior survives).

## Anti-scope — do NOT touch

- `gateway/run.py` shutdown sequence ordering (lines ~13000-13270) — no reordering
  of any step, even if it looks like an improvement. Additive persistence calls
  only, inserted without moving existing steps.
- `tools/async_delegation.py`'s own `async_delegations` table rows, its
  `completion_queue` producer calls (~line 923, ~1132), `restore_undelivered_completions()`,
  `mark_completion_delivered()`, `recover_abandoned_delegations()` — read these for
  the PATTERN only, do not modify this file's behavior.
- `completion_queue` consumer code in `gateway/run.py` (`_drain_gateway_watch_events`,
  ~line 18255) and `tui_gateway/server.py`'s equivalent (~line 9133).
- `cron/scheduler.py` — read-only, to verify behavior in a test; no production
  code changes there.
- Any TUI/RPC kill call sites (`tui_gateway/methods_tools.py:44`,
  `tui_gateway/server.py:12774`) or CLI kill call sites
  (`hermes_cli/cli_commands_mixin.py:467`) — these can continue calling `kill_all()`
  exactly as before; if your persistence write lives inside `kill_process()`/`kill_all()`
  itself these callers get the fix for free with zero code changes on their end,
  which is the intended outcome — just don't add anything call-site-specific to them.

## Discipline

- Root-cause each change against the real file:line cited above before writing
  code — these line numbers are correct as of 2026-08-09 but re-verify them
  yourself since the file may have shifted slightly; don't trust the numbers
  blindly, trust the actual grep.
- Build and test commands that must pass: `pytest tests/tools/test_process_registry.py -v`
  and `pytest tests/gateway/test_gateway_shutdown.py -v` and
  `pytest tests/cron/test_shutdown_interrupt.py -v` (all three existing suites,
  zero regressions — if any existing assertion must change because it previously
  asserted the BUGGY "not_found after restart" behavior as expected, that specific
  assertion may change, but you must call this out explicitly in your summary with
  a before/after diff of the assertion and why it was wrong).
- Commit, do NOT push. Deploy/restart verification is the orchestrator's separate
  step.
- New tests MUST assert the exact bug is gone: spawn a session, kill it (simulating
  shutdown kill), simulate a process restart (fresh registry instance recovering
  from the durable store, not the same in-memory object), then assert `poll()` on
  that session id returns the real terminal record (exit_code=-15,
  completion_reason="killed") — NOT `{"status": "not_found"}`.
