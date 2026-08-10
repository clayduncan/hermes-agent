
# Reviewer criteria: CRON_TASK_STATUS marker rebase

Score PASS only if ALL of the following hold. Check against actual repo state
(git diff, real test execution) — not the builder's self-report. This is the
same blast-radius class as Bug 1/Bug 2 (run_one_job decides status for every
cron job on the box) — hold this to the same bar.

1. **Full existing `tests/cron/` suite passes, zero regressions.** Run it
   yourself before trusting any self-report. If even one previously-passing
   test now fails, this is an automatic FAIL regardless of how correct the
   new feature looks in isolation.

2. **One-directional guard (hard requirement A) is enforced by an explicit
   guard in the code**, not an accidental consequence of ordering. Read the
   diff: the marker check must be inside an `if success:` (or equivalent
   explicit condition) block, so it structurally cannot execute when success
   is already False. Add/keep a test asserting a `CRON_TASK_STATUS: FAILED`
   marker present alongside an ALREADY-False success (e.g. a real framework
   error) does not get misattributed — the recorded error must still be the
   original framework failure, not the marker text.

3. **True no-op verified, not assumed (hard requirement B).** Confirm via a
   test that a final_response with NO marker line leaves success, error, AND
   delivered content byte-identical to pre-change behavior. This must be an
   actual test in the suite, not just an assertion in the spec.

4. **False-trigger guard test exists and passes (hard requirement C).** The
   test suite MUST include a case where the marker text appears in running
   prose (not as its own standalone line — e.g. embedded mid-sentence or with
   text preceding it on the same line) and does NOT trigger the override,
   alongside a case confirming a genuine standalone marker line DOES trigger
   it. If this distinction is not covered by an actual test that would fail
   without the `^...$` anchoring, FAIL the review — do not accept "the regex
   looks right" as sufficient.

5. **`_extract_cron_task_status` (or equivalently-named helper) is a pure
   function with no side effects**, callable and testable in isolation
   (verify by reading the diff — it should not mutate `job`, `success`, or
   any shared state itself; the caller in `run_one_job` does the mutation).

6. **Existing overrides are untouched in behavior.** Read the diff around the
   shutdown-interruption override, empty-response override, blocked-config
   handling, and silence-suppression. None of their trigger conditions or
   ordering relative to each other should have changed — only the NEW check
   should be added, composing with them.

7. **Scope discipline.** `git diff --name-only` must show changes ONLY to
   `cron/scheduler.py` and `tests/cron/test_run_one_job.py` (or wherever the
   ported tests actually land after reading the current test file structure).
   No changes to `~/.hermes/scripts/`, `hermes_r2_backup.sh`,
   `hermes_r2_restore_test.sh`, or any file outside `hermes-agent/cron/` and
   its tests.

8. **No push occurred.** This stays local — verify no `git push` was run as
   part of the build (check shell history in the builder's transcript if
   available, or just confirm origin's remote HEAD is unchanged).

If criterion 1's full-suite proof is missing or the builder only ran the new
tests in isolation without the full `tests/cron/` suite, FAIL the review
regardless of how clean the new code looks — this function decides status for
every cron job on the box and a regression here is exactly the kind of
silent-failure class this whole track has been fixing.
