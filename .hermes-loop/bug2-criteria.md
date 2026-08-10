# Reviewer criteria: Bug 2 — durable exactly-once completion notifications

Score PASS only if ALL of the following hold. Check against actual repo state
(git diff, test output, live code reading) — not the builder's own summary.
This mirrors the Bug 1 review discipline: revert the fix and confirm the new
headline test(s) actually fail against pre-fix code, don't just take a green
suite at face value.

## MUST — correctness of the core fix

1. **A test proves the exact original bug is gone**: an event enqueued for
   delivery, process death simulated BEFORE delivery is confirmed (a genuinely
   fresh state — new registry/store instance, not the same in-memory objects),
   then a startup-replay call is invoked, and the event IS delivered
   (re-appears on a fresh `completion_queue`, or the equivalent replay target
   the builder chose). Verify by reverting to the pre-Bug-2 commit and
   confirming this exact test fails there. FAIL if the test reuses the same
   in-memory queue/registry without a genuine restart simulation.

2. **Exactly-once is proven with THREE separate tests, not one:**
   a. A delivered event is never replayed (mark delivered, restart-simulate,
      assert NOT redelivered).
   b. A pending (undelivered) event is redelivered exactly once, not zero
      times — the core bug.
   c. Recovery/replay running twice in a row (idempotency check — e.g. startup
      replay called twice, or a retried startup) does not deliver the same
      event twice.
   FAIL if any of these three is missing or if any asserts something other
   than exact-once (e.g. only checks "at least once" without also checking
   "not more than once").

## MUST — the four protected constraints (each needs its own passing test, not just a code-read claim)

3. **`delegate_task`/`async_delegation` producer path is untouched.** A test
   puts an `async_delegation`-typed event onto `completion_queue` and asserts:
   (a) it is NOT written to any new durable store this build adds, (b) the
   existing requeue-and-skip behavior in `_drain_gateway_watch_events()` for
   `evt_type == "async_delegation"` is unchanged (still requeues, still
   skipped by this drain), (c) `tools/async_delegation.py` shows ZERO diff
   in `git diff --name-only`. FAIL if the file is modified at all, or if the
   test is missing, or if it only asserts (c) without (a) and (b).

4. **Queue-depth semantics for readiness/health are correct or updated in
   lockstep.** Either: (a) a test shows `completion_queue.qsize()` after a
   normal persist+enqueue+deliver cycle returns the same value it would have
   pre-change (semantics fully preserved), OR (b) `gateway/readiness.py` and
   `gateway/platforms/api_server.py` were modified to account for the new
   semantics, with the builder's summary explicitly explaining what changed
   and why the new number is still meaningful for health reporting. FAIL if
   neither — i.e. if depth semantics silently shifted with no test proving it
   and no lockstep update.

5. **`cron/scheduler.py` shows ZERO diff** (`git diff --name-only`), and both
   `tests/cron/test_shutdown_interrupt.py` and
   `tests/cron/test_shutdown_interrupt_terminal_state.py` pass UNMODIFIED (diff
   these two specific test files against their Bug-1-era version — they should
   show zero changes, since Bug 2 doesn't touch cron behavior at all). FAIL if
   `cron/scheduler.py` is touched, or if either test file needed changes to
   keep passing (that would mean Bug 2's change altered cron-relevant
   behavior it wasn't supposed to touch).

6. **`gateway/run.py`'s shutdown-sequence region (~lines 13000-13270, the
   kill-before-disconnect ordering) has zero reordering.** Any new lines added
   to this file must be either (a) entirely outside this region (e.g. near
   startup/`recover_from_checkpoint()`), or (b) surgically additive within the
   drain function (`_drain_gateway_watch_events`) without moving any existing
   line relative to another existing line. Read the actual diff yourself and
   confirm no existing line's relative order changed. FAIL if any line in the
   13000-13270 region shows as moved/reordered rather than purely additive.

## MUST — anti-scope compliance

7. **Zero changes to**: `tools/async_delegation.py`, `cron/scheduler.py`,
   `tui_gateway/methods_tools.py`, `tui_gateway/server.py`,
   `hermes_cli/cli_commands_mixin.py`. Grep `git diff --name-only` for all
   five — FAIL if any appear.

8. **Bug 1's shipped code is not refactored/renamed as part of this build.**
   `git diff` on `tools/process_terminal_store.py` and the Bug-1 hookups in
   `tools/process_registry.py` (the `_record_terminal_state`,
   `_poll_from_terminal_store`, `_restore_terminal_states`,
   `_session_from_terminal_record` methods and their call sites) should show
   ONLY additive changes (new columns/functions alongside, or new call sites
   consuming them) — not modification of their existing logic or signatures.
   FAIL if any Bug-1 function's existing behavior/signature changed rather
   than being purely extended.

## MUST — regression floor

9. **All six suites pass**: `tests/tools/test_process_registry.py`,
   `tests/tools/test_process_terminal_store.py`,
   `tests/gateway/test_gateway_shutdown.py`,
   `tests/gateway/test_background_process_notifications.py`,
   `tests/cron/test_shutdown_interrupt.py`,
   `tests/cron/test_shutdown_interrupt_terminal_state.py`. Run them yourself,
   don't trust the builder's report. If any existing assertion changed,
   require a before/after explanation in the summary tied to why the old
   assertion encoded incorrect behavior — FAIL if unexplained.

## FAIL conditions (any one fails the whole review regardless of other criteria)

- Any of the four protected-constraint tests (3-6) missing or not actually
  proving what it claims.
- `async_delegation.py` modified at all.
- `cron/scheduler.py` modified at all.
- Shutdown-sequence kill-before-disconnect ordering changed.
- Exactly-once not proven by all three required tests (criterion 2).
- No live-restart-shaped test for the core bug (criterion 1).
- `git push` was run (commit only).

## Anti-scope list (explicit, for the reviewer's grep)

Files that must show ZERO diff versus the Bug-1-fix commit:
- `tools/async_delegation.py`
- `cron/scheduler.py`
- `tui_gateway/methods_tools.py`
- `tui_gateway/server.py`
- `hermes_cli/cli_commands_mixin.py`
- `tests/cron/test_shutdown_interrupt.py`
- `tests/cron/test_shutdown_interrupt_terminal_state.py`

No Prisma/SQL migration-runner files touched.
