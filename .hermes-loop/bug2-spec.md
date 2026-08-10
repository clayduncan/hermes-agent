# Spec: durable + exactly-once completion notifications for background process sessions

## Root cause (confirmed via live incident 2026-08-09, diagnosis already done — do not re-diagnose)

`ProcessRegistry._move_to_finished()` (tools/process_registry.py, ~line 1538-1567)
enqueues a completion event onto `self.completion_queue` when
`session.notify_on_complete` is set (~line 1563-1573). The gateway drains that
queue after every agent turn via `_drain_gateway_watch_events()`
(gateway/run.py ~line 3433, called from ~line 18255) and injects completions as
synthetic `[IMPORTANT: ...]` inbound messages through the platform adapter
(e.g. Telegram).

If the platform adapter has already disconnected — part of the SAME gateway
shutdown sequence, observed ~10ms before the kill in the original incident —
before the queue is drained, the enqueued event has no consumer left. The
gateway then exits; `completion_queue` is an in-memory `queue.Queue`, so the
event is garbage-collected with the process. No redelivery exists on the next
boot. This is DISTINCT from Bug 1 (already fixed — see
`tools/process_terminal_store.py` and the corresponding hookups in
`tools/process_registry.py`): Bug 1 makes `poll()` correct after a restart;
Bug 2 is about the actual NOTIFICATION (the synthetic inbound message a user
would see) never arriving, which can happen even for a session that exited
cleanly with no kill involved, any time a restart race lands between enqueue
and drain.

## What to build

Durable persistence + exactly-once replay for `completion_queue` events
produced by `ProcessRegistry`, so a completion event survives a gateway
process death between enqueue and delivery, and is redelivered exactly once
on the next boot — not zero times (silently lost, the original bug) and not
twice (a duplicate `[IMPORTANT: ...]` message reporting the same job as
finished again).

**Reuse the store pattern from Bug 1** (`tools/process_terminal_store.py`,
already built and proven — read it in full). Extend it (add a
`delivered_at`/`delivery_state` column to a new table in the SAME
`processes.db` file, or a new small table — your call) rather than inventing a
new persistence mechanism. Do NOT reuse `async_delegation.py`'s
`async_delegations` table or its `_DB_LOCK` for this — same isolation
reasoning as Bug 1: `process_registry` completions must never serialize
behind `delegate_task` delivery-claim I/O.

**Mechanics, at a minimum:**
1. When `_move_to_finished()` would enqueue a completion event (the
   `notify_on_complete` branch, ~line 1563-1573), ALSO persist that event
   durably first, with a `pending` delivery state, THEN put it on
   `completion_queue` as today.
2. On gateway startup (near where `recover_from_checkpoint()` already runs,
   gateway/run.py ~line 11165, or inside `ProcessRegistry.__init__`/its own
   recovery call — your call which layer owns this), query the durable store
   for any `pending` completion events and re-enqueue them onto
   `completion_queue` exactly as `restore_undelivered_completions()` in
   `async_delegation.py` does for delegation completions (read that function,
   ~line 344-368, for the exact pattern: mark `restored=True` in-memory only,
   never persisted, so a replayed event is visibly distinguishable if needed
   but not treated specially by anything downstream that doesn't care).
3. Mark an event `delivered` in the durable store at the point delivery is
   actually confirmed — i.e. where `_format_gateway_process_notification()`
   (gateway/run.py ~line 3401) successfully produces the synthetic message
   AND it's actually handed to the injection path, not merely dequeued. If you
   cannot get a true delivery confirmation at that exact point without
   touching code outside this spec's scope, mark it delivered at the point of
   successful dequeue+format in `_drain_gateway_watch_events()` — this is
   "at-least-once until confirmed, but never redelivered after this point,"
   the same tradeoff `async_delegation.py`'s `mark_completion_delivered()`
   makes. State your choice and why in the summary.

## Exactly-once requirement (hard, from spec — this is the crux of Bug 2)

A completion event must be delivered exactly once across a restart:
- **Not zero times**: the original bug. A pending event must survive process
  death and be replayed on the next boot.
- **Not twice**: an event already drained and formatted into a message before
  the process died must NOT be replayed just because it was still sitting in
  the in-memory `completion_queue` when persistence was written (i.e. don't
  persist-then-never-mark-delivered such that every boot replays every
  historical completion forever). Use a bounded retention window (mirror
  `process_terminal_store.py`'s `RETENTION_SECONDS`/`MAX_RETAINED` constants —
  reuse those exact values unless you have a specific reason to diverge, state
  it if you do) AND a delivery-state flag, the same two-part guard
  `async_delegation.py` already uses (`delivery_state != 'delivered'` filters
  the replay query, retention/pruning bounds the table).
- Write a test that proves this directly: persist a pending event, mark it
  delivered, restart-simulate, assert it is NOT replayed. Separately: persist
  a pending event, do NOT mark it delivered, restart-simulate, assert it IS
  replayed exactly once (not duplicated if recovery runs twice, e.g. from a
  flaky retry of the startup call itself — test that recovery is idempotent).

## Hard constraints (all four are load-bearing, verify with a dedicated test each)

### 1. `delegate_task` notification behavior is UNCHANGED for `async_delegation.py`'s own producer

`async_delegation.py` enqueues onto this SAME `completion_queue` object
(search for its own `.put(` calls onto the queue it's handed — do not assume
the line numbers from an earlier investigation are still accurate, re-grep).
Whatever persistence/replay mechanism you add to `ProcessRegistry` must be
scoped so it NEVER intercepts, delays, persists, or replays an
`async_delegation`-sourced event. `_drain_gateway_watch_events()` already
requeues `evt_type == "async_delegation"` events untouched (~line
3453-3454) — that requeue-and-skip behavior must remain byte-identical.
Write a test asserting an `async_delegation`-typed event put on the queue is
NOT written to your new durable store and NOT touched by your new drain/replay
code path at all.

### 2. Queue-depth semantics for readiness/health checks stay correct

`gateway/platforms/api_server.py` (~line 1593) and `gateway/readiness.py`
(~line 94, 111-116) read `process_registry.completion_queue.qsize()` directly
for health reporting. If your change alters how/when items enter or leave the
in-memory queue (e.g. deferring the `.put()` until after a durable write
completes, or buffering replayed events differently than fresh ones), the
depth these checks report must still reflect genuinely-pending work — not
under-report (hiding a real backlog) or over-report (a persisted-but-already-delivered
event double-counted). If preserving today's exact semantics isn't possible
given the design you choose, update `readiness.py`/`api_server.py` in lockstep
and say so explicitly in your summary — do not leave them silently reading a
number that no longer means what the healthcheck assumes it means. Write a
test asserting `qsize()` after a normal enqueue+deliver cycle matches today's
behavior.

### 3. `cron/scheduler.py`'s `mark_running_jobs_interrupted` ordering dependency still holds

This was already verified as unaffected by Bug 1 (read-only, unmodified,
regression test in `tests/cron/test_shutdown_interrupt_terminal_state.py`
passing). Bug 2 must not disturb it either, since Bug 2 touches the same
`_kill_tool_subprocesses` neighborhood conceptually (both fire during
shutdown) even though the actual code paths are different (kill vs. drain).
`cron/scheduler.py` must show ZERO diff. Re-run
`tests/cron/test_shutdown_interrupt.py` and
`tests/cron/test_shutdown_interrupt_terminal_state.py` (both must still pass
unmodified) as part of your own validation before reporting.

### 4. The kill-before-disconnect ordering in `gateway/run.py` stays untouched

Same constraint as Bug 1 — `_kill_tool_subprocesses("post-interrupt")` before
adapter teardown, `_kill_tool_subprocesses("final-cleanup")` after — for the
same #8202 reason. Bug 2's OWN work (drain persistence, startup replay) is
naturally a different piece of code than the kill calls, but if you touch
`gateway/run.py`'s shutdown sequence at all (e.g. to add a persistence-write
call at the drain point, or a replay call at startup), any added lines must
be additive/surgical — no reordering of existing lines relative to each
other. `gateway/run.py`'s startup path (where `recover_from_checkpoint()` is
called, ~line 11165) is a DIFFERENT region than the shutdown sequence
(~13000-13270) and is fair game for adding a replay call — that's expected
and in-scope. The shutdown-sequence region specifically is what must stay
ordering-stable.

## Scope fence — files this build is expected to touch

- `tools/process_terminal_store.py` — extend with the delivery-tracking table
  and its read/write functions, OR a new sibling module in the same
  directory if you judge that cleaner (your call, state which and why).
- `tools/process_registry.py` — hook the persist-before-enqueue and
  delivered-marking calls into `_move_to_finished()` and wherever delivery is
  confirmed.
- `gateway/run.py` — a startup replay call near existing recovery logic
  (~line 11165 region), and possibly a delivered-confirmation hook inside or
  near `_drain_gateway_watch_events()`/`_format_gateway_process_notification()`
  (~line 3401-3458) IF that's where you choose to mark delivery (per the
  "at-least-once until confirmed" note above). The shutdown-sequence region
  (~13000-13270) must NOT be touched by this spec.
- New/updated tests under `tests/tools/test_process_registry.py`,
  `tests/tools/test_process_terminal_store.py` (or its sibling), and a new
  regression test file if warranted for the readiness/api_server depth
  semantics and the async_delegation isolation guarantee.

## Anti-scope — do NOT touch

- `tools/async_delegation.py` — must remain fully unmodified. Read it for the
  pattern only, exactly as in Bug 1.
- `cron/scheduler.py` — read-only, verify via test, no production code
  changes.
- `gateway/run.py`'s shutdown sequence lines ~13000-13270 (the kill/adapter
  teardown ordering) — no reordering there. Additions elsewhere in the file
  (startup replay, drain-point persistence hook) are in-scope per above.
- `tui_gateway/methods_tools.py`, `tui_gateway/server.py`,
  `hermes_cli/cli_commands_mixin.py` — unmodified, same as Bug 1.
- Any change to `Bug 1`'s existing terminal-state persistence behavior
  (`record_terminal_state`, `get_terminal_state`, `recover_terminal_states`,
  and their call sites in `process_registry.py`) beyond what's needed to
  add the NEW delivery-tracking columns/table alongside them. Do not refactor
  or rename anything already shipped for Bug 1 as part of this build.

## Discipline

- Root-cause each change against the real file:line cited above before
  writing code — re-verify every line number yourself via grep, since Bug 1's
  build already shifted some of `process_registry.py`'s line numbers from
  what an earlier investigation cited.
- Build and test commands that must pass:
  `pytest tests/tools/test_process_registry.py tests/tools/test_process_terminal_store.py tests/gateway/test_gateway_shutdown.py tests/gateway/test_background_process_notifications.py tests/cron/test_shutdown_interrupt.py tests/cron/test_shutdown_interrupt_terminal_state.py -v`
  — zero regressions across all six suites.
- Commit, do NOT push. Deploy/restart verification is the orchestrator's
  separate step.
- New tests MUST assert the exact bug is gone AND exactly-once holds: (a) a
  pending event survives a simulated restart and is replayed exactly once;
  (b) a delivered event is never replayed; (c) recovery running twice (e.g. a
  double-call at startup) does not duplicate delivery; (d) an
  `async_delegation`-sourced event on the same queue is never touched by any
  of this new code; (e) `completion_queue.qsize()` after a normal cycle
  matches pre-change behavior (or `readiness.py`/`api_server.py` were updated
  in lockstep, with that update explicitly called out).
