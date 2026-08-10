
# Spec: rebase forward the CRON_TASK_STATUS marker feature (08-08 stash)

## Background

A stash from 2026-08-08 (`hermes-update-autostash-20260808-122732`, stash@{0}
in this repo) contains a real, tested feature that never landed: an opt-in
`CRON_TASK_STATUS:` marker letting a cron job's own final response declare its
task outcome, overriding an otherwise-"successful" agent turn. The stash no
longer applies cleanly (`git apply --check` fails at `cron/scheduler.py:3975`)
because the file has moved significantly since 08-08 (today's Bug 1/Bug 2 work
landed a shutdown-interruption override, blocked-config handling, and other
logic in between). This task is to re-implement the SAME feature against
CURRENT `cron/scheduler.py`, not to force-apply the stale patch.

## The gap being closed (confirmed by reading current code, do not re-diagnose)

`run_one_job` in current `cron/scheduler.py` (~line 4503) already has several
overrides that downgrade `success=True` to `success=False`:
- Shutdown-interruption override (~line 4609): `_is_interrupted(job["id"])`
- Empty-response override (~line 4685): blank `final_response.strip()`
- Blocked-config handling and silence suppression (separate paths)

ALL of these trigger on a FRAMEWORK-level signal (interruption flag, empty
string, config-block marker). None of them let the AGENT's own final response
TEXT declare "the task I was asked to do failed" when the turn completed
normally with a real, non-empty response. E.g. a cron job whose prompt is
"check X, report OK or FAILED" that concludes FAILED but writes a full,
well-formed response is recorded as `last_status: "ok"` today. That is the
gap. This feature closes it with an opt-in text marker.

## What to build

Re-implement the stash's design against current HEAD:

1. A helper function (name it `_extract_cron_task_status`, matching the
   stash's original name so intent is traceable) that scans a final-response
   string for a `CRON_TASK_STATUS:` marker line and returns its declared value
   (`OK` / `FAILED` / `FINDING`) or `None` if absent. Original stash regex:
   `r"^CRON_TASK_STATUS:\s*(OK|FAILED|FINDING)\s*$"` with
   `re.IGNORECASE | re.MULTILINE`.

2. A check in `run_one_job`, inserted after `final_response` has been fully
   computed (i.e. after the empty-response/explainer-suppression logic around
   line 4685, in the same region as the existing interruption override) that:
   - Only fires when `success` is currently `True` (one-directional: can only
     turn ok->error, never touches an already-failed run — see hard
     requirement below).
   - Calls the extractor on `final_response`.
   - If the extracted value is `FAILED`, sets `success = False` and `error =
     "Job reported CRON_TASK_STATUS: FAILED"` (or equivalent wording).
   - If the extracted value is `OK`, `FINDING`, or `None` (no marker), leaves
     `success` and `error` completely unchanged — TRUE no-op.

3. Port forward the stash's 4 existing tests into
   `tests/cron/test_run_one_job.py`, updated to match current test-fixture
   helpers (e.g. whatever `_patch_pipeline`-equivalent harness exists in the
   CURRENT test file — read the current file first, do not assume the stash's
   helper signature still matches).

## Hard requirements (all three are non-negotiable acceptance gates)

### A. One-directional guard
The marker check must ONLY execute when `success` is currently `True` entering
the check. If `success` is already `False` from any other cause (framework
failure, interruption, empty response, blocked config), the marker check must
be skipped entirely — a `CRON_TASK_STATUS: FAILED` marker must never be
evaluated in that case, and a hypothetical `CRON_TASK_STATUS: OK` marker
appearing in a failure explanation/summary must NEVER flip a real failure back
to success. Write this as an explicit early-exit/guard in the code, not an
implicit consequence of control flow that could break under a later edit.

### B. True no-op for non-marker jobs
Every existing job that never emits the marker must have byte-identical
`success`/`error`/delivered-content/`last_status` behavior after this change.
This means: the extractor must return `None` on any final_response that does
not contain a marker line, and `None` must be treated as "leave everything
untouched" — not merely "leave success untouched but touch error or
delivered content." Verify this is real, not assumed, by running the FULL
existing `tests/cron/` suite before and after and diffing pass/fail counts —
zero regressions permitted.

### C. No false-trigger on passing mention
The regex must anchor the marker to its OWN LINE (`^...$` with MULTILINE,
matching the stash's original anchoring) so a response that merely MENTIONS
the marker in prose — e.g. "I checked whether the job emits a
CRON_TASK_STATUS: FAILED line but it never does" as a sentence, or
"CRON_TASK_STATUS: FAILED is the marker format we use" embedded mid-paragraph
— does NOT false-trigger. The stash's existing 4 tests do NOT cover this case.
ADD a new test that asserts a response which mentions the marker syntax in
running prose (NOT as its own standalone line, e.g. with trailing punctuation
or embedded in a sentence with text before it on the same line) does NOT
trigger the override, alongside a case that confirms an actual standalone
marker line still fires. Do not rely on the anchoring being "obviously
correct" — write the test that proves it against the actual regex shipped.

## Constraints

- Blast radius: `run_one_job` decides status for EVERY cron job on the box
  (Green/Amber/Red tiering, all backup jobs, all watchdogs, everything under
  `cron/`). This is the same class of change as today's Bug 1/Bug 2 fixes —
  treat it with the same care. Read the full current `run_one_job` function
  before editing, not just the diff region.
- Do not touch the shutdown-interruption override, empty-response override,
  blocked-config handling, or silence-suppression logic — those are correct
  today and out of scope. The new check must compose with them, not replace
  or reorder them in a way that changes their existing behavior.
- Do not touch `hermes_r2_backup.sh`, `hermes_r2_restore_test.sh`, or anything
  under `~/.hermes/scripts/` — unrelated to this task, already fixed and
  verified separately today.
- No push to any remote. This stays local, same as today's other fixes.
