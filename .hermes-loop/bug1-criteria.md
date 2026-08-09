# Reviewer criteria: Bug 1 — persist terminal state for killed background sessions

Score PASS only if ALL of the following hold. Each criterion must be checked
against actual repo state (git diff, test output, live code reading) — not
against the builder's own summary.

## MUST — correctness

1. **A new/updated test exists that reproduces the exact original bug and proves
   it is fixed.** The test must: spawn a session, kill it via the kill path
   (`kill_process()` or `kill_all()`), then construct or simulate a state that
   represents "a fresh gateway process after restart" (a genuinely new registry
   instance recovering from durable storage, NOT reusing the same in-memory
   `_running`/`_finished` dicts from the same test's earlier spawn call), and
   assert `poll()` on that session id returns `exit_code=-15` and
   `completion_reason="killed"` (or equivalent real terminal values) — NOT
   `{"status": "not_found"}`. FAIL if the test reuses the same registry instance
   without a genuine "restart" simulation step — that would prove nothing about
   the actual bug (in-memory state surviving in the SAME process was never the
   problem).

2. **The builder's summary explicitly states and justifies which reuse option was
   chosen** (import/reuse `async_delegation.py`'s SQLite machinery in a separate
   table, vs. build a `process_registry`-scoped equivalent) with reasoning that
   references the actual code read, not a generic explanation. FAIL if the
   summary doesn't address this or gives a reason unconnected to what's actually
   in either file.

## MUST — the three non-negotiable constraints from spec.md

3. **`gateway/run.py`'s kill-before-disconnect ordering is byte-identical to
   before this change**, specifically: `_kill_tool_subprocesses("post-interrupt")`
   still runs before the adapter-teardown loop, and `_kill_tool_subprocesses("final-cleanup")`
   still runs after it, with no lines reordered, removed, or moved relative to
   each other or to adapter teardown. `git diff` on `gateway/run.py` in the range
   covering the shutdown sequence (roughly lines 13000-13270, re-verify actual
   line numbers) should show ONLY additive lines (new persistence-write calls) or
   surgical single-line insertions — zero moved/reordered existing lines. FAIL if
   any existing line in this range changed position relative to another existing
   line in this range.

4. **The persistence write added to the kill path is cheap and does not introduce
   network I/O, cross-await lock holding, or O(n) scaling with running-session
   count on the `run_agent.py:4283` hot call site.** Read the actual diff to
   `kill_process()`/`kill_all()`/wherever the write was added, confirm it's a
   single bounded local write (SQLite insert/update on one row, or equivalent),
   and confirm the builder's summary includes explicit reasoning about this cost
   (not just an assertion of "it's fine"). FAIL if the write does anything
   unbounded, any remote call, or if the summary is silent on cost.

5. **A regression test exists asserting `cron/scheduler.py`'s `mark_running_jobs_interrupted`
   behavior (cron job killed during shutdown → marked interrupted, not falsely
   reported as successful) still holds after this change**, and this test is
   NEW or the existing `tests/cron/test_shutdown_interrupt.py` suite passes
   unmodified (or with justified changes only, per criterion 7). FAIL if no such
   test exists or if `cron/scheduler.py` itself was modified (it should be
   read-only per the anti-scope).

## MUST — anti-scope compliance

6. **Zero changes to any of the following** (grep the diff for these paths/symbols,
   FAIL if any appear as modified):
   - `tools/async_delegation.py` — the file must be completely unmodified (a
     `git diff --name-only` must NOT list this file at all).
   - `completion_queue` producer/consumer logic anywhere (search the diff for
     the string `completion_queue` — any occurrence in a MODIFIED line, not just
     an added comment referencing it, is a FAIL).
   - `_drain_gateway_watch_events` in `gateway/run.py`.
   - `tui_gateway/methods_tools.py`, `tui_gateway/server.py`,
     `hermes_cli/cli_commands_mixin.py` — must be unmodified (not in
     `git diff --name-only`).
   - `cron/scheduler.py` — must be unmodified (not in `git diff --name-only`).

## MUST — regression floor

7. **All three existing test suites pass**: `tests/tools/test_process_registry.py`,
   `tests/gateway/test_gateway_shutdown.py`, `tests/cron/test_shutdown_interrupt.py`.
   If any existing assertion had to change, the builder's summary must show the
   before/after of that specific assertion and explain why the old assertion
   encoded the bug (e.g. "previously asserted poll() returns not_found after a
   kill — this was the bug itself, not correct behavior"). FAIL if any suite
   fails, or if an assertion changed with no explanation.

## FAIL conditions (any one of these fails the whole review regardless of other criteria)

- Any file outside the scope-fence list in spec.md was modified, unless the
  builder's summary explicitly justifies it as a necessary and correct touch
  (per the general "surprise file" allowance) — read that justification yourself
  and judge whether it's genuinely necessary or scope creep.
- `async_delegation.py` modified at all.
- `completion_queue` delivery/draining logic touched at all.
- Shutdown-sequence ordering changed.
- No live-restart-shaped test (criterion 1) exists — a green suite with only
  in-process/in-memory tests that never simulate a restart does NOT satisfy this
  spec, since the bug IS about surviving a restart.
- `git push` was run (commit only, never push, per Discipline in spec.md).

## Anti-scope list (explicit, for the reviewer's grep)

Files that must show ZERO diff:
- `tools/async_delegation.py`
- `cron/scheduler.py`
- `tui_gateway/methods_tools.py`
- `tui_gateway/server.py`
- `hermes_cli/cli_commands_mixin.py`

No Prisma/SQL migration files should be touched (this repo may not use Prisma —
if it does, confirm zero migration files in the diff; if it's plain SQLite via
`sqlite3`/similar, confirm no schema-migration-runner file was touched either).
