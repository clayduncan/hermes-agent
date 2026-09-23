# OPS-114: Team Duncan Desk/Plaud Automation (Operations)

Status as of this build: **code and tests only**. Nothing here is scheduled,
registered, or activated. Every item under "Activation" below is a
separate, Clay-controlled step.

## 1. What this build adds

- A non-interactive automation entry point (`automation_runner.py`) with
  three modes: `desk`, `plaud-reconcile`, `plaud-webhook`. It never touches
  the interactive `prepare_call_log_ingest` / `confirm_call_log_ingest` /
  `accept_call_log_ingest_run` / `prepare_plaud_summary_run` /
  `confirm_plaud_summary_run` tools or their confirmation-token tables: it
  calls straight into the same `IngestionRunner` / `PlaudSummaryRunner`
  seams those tools use.
- A shared durable process lock (`process_lock.py`), a single lock file
  under `<hermes_home>/cron/locks/team-duncan-automation.lock`, used by
  every automated mode *and* by the interactive confirm/accept handlers
  once execution begins (see §6).
- Content-free heartbeat markers (`heartbeat.py`), one JSON file per source
  under `<hermes_home>/plugin-data/team_duncan_contacts/automation_heartbeats/`
  (`desk.json`, `plaud_reconcile.json`, `plaud_webhook.json`).
- A standalone, narrowly-scoped Plaud webhook receiver
  (`plaud_webhook_receiver.py`) with real HMAC authentication, no
  insecure bypass mode, and a durable SQLite acceptance queue
  (`webhook_queue.py`, see §5) so an accepted event survives a crash of
  the receiver process.
- Scripts-repo wrappers (`team_duncan_ops114_report.py`,
  `team_duncan_ops114_desk_cron.sh`, `team_duncan_ops114_plaud_reconcile_cron.sh`)
  that route through the existing `cron_report.py` / `cron_ledger.py`
  Green/Amber contract.
- A watchdog extension (`team_duncan_ops114_watchdog.py`), chained into the
  existing `cron_watchdog_cron.sh` the same way
  `hindsight_circuit_red_watchdog.py` is chained today.

## 2. Present manual baseline (unchanged by this build)

- **Desk**: `prepare_call_log_ingest` issues a single-use 5-minute token
  (no source read), rejects cron/background context, and blocks if an
  earlier run is still awaiting acceptance. `confirm_call_log_ingest`
  consumes the token and runs ingestion, leaving the run
  `awaiting_acceptance`. `accept_call_log_ingest_run` clears that hold.
  Notification retries are only processed during a confirmed run.
- **Plaud**: `prepare_plaud_summary_run` issues its own single-use
  5-minute token (no source read), rejects cron/background context.
  `confirm_plaud_summary_run` consumes it and runs match, transcript
  retrieval, summarization, and in-place note enrichment. There is no
  acceptance step.
- All five interactive tools remain registered, continue to reject
  cron/background invocation, and still work exactly as before. This build
  adds nothing that can create or consume their confirmation tokens from a
  scheduled path (see the flagship test:
  `tests/plugins/team_duncan_contacts/test_automation_runner.py::test_desk_mode_never_touches_confirmation_token_or_run_tables`).

## 3. Seven-day retention: findings and cost

Live read-only proof on 2026-09-22: Desk retains 2,260 rows from
2022-03-26T23:17:52Z through 2026-09-22T23:20:41Z. 79 rows are inside the
current 7-day routine window; 2,181 are older. The last Desk cursor
(2026-09-21T22:29:17Z) is still retained. **Proven irrecoverable loss is
zero.** History outside the 7-day window is unscanned but retrievable
(this build does not backfill it).

Cost: every 15-minute Desk tick re-reads up to 7 days of metadata before
cursor/dedupe filtering (at the measured 79-row window and 96 ticks/day,
about 7,584 metadata-row reads/day). This buys recovery from delayed ticks
without widening the routine scan; it does not imply the source itself has
aged anything out.

## 4. Schedules (not registered by this build)

| Job | Schedule | Script | Absence threshold |
|---|---|---|---|
| OPS-114 Team Duncan Desk Ingestion | every 15 min (`*/15 * * * *`) | `team_duncan_ops114_desk_cron.sh` | 45 min |
| OPS-114 Team Duncan Plaud Reconciliation | every 3 hours (`0 */3 * * *`) | `team_duncan_ops114_plaud_reconcile_cron.sh` | 4 hours |

See `scripts/team_duncan_ops114_cron_manifest.json` for the exact
`hermes cron create ... --no-agent` commands. `--no-agent` means the
script *is* the job: no LLM involved, matching the "no LLM" requirement
for these two schedules. This build does not run either command.

45 minutes = three expected 15-minute cadences, chosen to survive one
overlap skip and one delayed tick. 4 hours = one hour of grace beyond the
3-hour Plaud cadence.

The per-tick wrapper's own process exit code (what the cron scheduler
sees) is 0 for both a completed run and a skipped-lock run: a clean
live-owner lock collision is not an ingestion failure, so it must not
surface as one to Hermes's `--no-agent` cron path (which treats any
nonzero exit as failure). It is still routed to `cron_report.py` as Green
either way. Only a crashed or genuinely failed run exits 1.

## 5. Plaud webhook: current truth

There is no proven native signed personal-data webhook from Plaud. The
supported primary trigger is Plaud's documented Zapier **"Transcript &
Summary Ready"** trigger, mapped to a normalized custom POST, authenticated
at `plaud_webhook_receiver.py` with HMAC (the same generic V2 scheme,
`X-Webhook-Signature-V2` / `X-Webhook-Timestamp`, HMAC-SHA256 over
`"<timestamp>.<body>"`, 300s replay tolerance, already used by
`gateway/platforms/webhook.py`, so this receiver's auth is consistent with
the rest of the fleet even though it is not a route on that adapter). Do
not claim a native Plaud signature anywhere downstream of this doc.

**Until Clay activates the Zapier mapping and secret (§7), the 3-hour
Plaud reconciliation job is the only live Plaud path**, and remains
authoritative recovery even after activation (a missed/failed webhook
delivery is caught by the next reconciliation tick, which scans by
frontier position, not by webhook delivery).

The receiver trusts only the immutable `plaud_recording_id` from the
webhook body: title, transcript text, speaker names, and summary are
never read from it, even if present (`normalize_zapier_payload`). All
actual processing metadata comes from a sealed `fetch_by_identity`
re-fetch inside `PlaudSummaryRunner.process_one()`, the same
never-trust-the-push-payload discipline every other sealed re-fetch in
this plugin already uses.

Durable acceptance: before the receiver returns HTTP 202, the immutable
recording ID is committed to a small SQLite queue (`webhook_queue.py`)
under Team Duncan plugin data, deduped by recording ID. A 202 is a
crash-survival promise, not just an in-memory one; if the durable write
itself fails, the receiver returns 503 instead, never 202. Once queued,
the event is dispatched to `automation_runner.run(["plaud-webhook", ...])`
on a background thread as a best-effort fast path (sharing the same
OPS-114 durable lock every automated mode uses) and removed from the queue
only once that run actually completes. `drain_pending_events()`, called at
this receiver's own process startup, replays anything still pending from
before a prior crash of this process, so an event accepted just before a
crash is never silently lost.

## 6. The shared lock and operator overrides

One durable lock, `TeamDuncanLock`, at
`<hermes_home>/cron/locks/team-duncan-automation.lock`: a single file held
with kernel `fcntl.flock(LOCK_EX | LOCK_NB)`, with content-free PID/mode
metadata written into it purely for operator inspection (never consulted
to decide whether the lock is free). Crash safety is a kernel guarantee,
not a heuristic: when a process holding the lock dies, by any means, up to
and including a hard kill with no cleanup code ever running, the kernel
releases that flock as part of tearing down the process's file
descriptors. There is no age check and no PID-liveness check deciding
whether to steal the lock, because there is nothing left to steal by the
time a dead owner's flock is gone; a leftover lock file with stale
metadata on disk never blocks a fresh acquire.

Every automated mode acquires the lock before touching `ingestion_state.db`.
`confirm_call_log_ingest`, `accept_call_log_ingest_run`, and
`confirm_plaud_summary_run` now acquire the same lock before their own
mutation: a live-owner collision (e.g. an automated run in progress) is a
clean `{"status": "rejected", "reason": "automation_lock_active"}`, and the
interactive confirmation token is **not** consumed, so Clay can just retry
within the token's remaining TTL. A scheduled run that loses the race gets
a clean `skipped_lock` outcome (no exception, no partial state), and its
heartbeat marker's `last_attempt_at` advances but `last_success_at` does
not.

Manual tools remain full operator overrides: they still work interactively
at any time (subject only to the same lock a concurrent automated run
might be holding), still reject cron/background context, still use
5-minute single-use tokens, and a scheduled path can never create or
consume one of those tokens.

## 7. Activation instructions (Clay-controlled, not part of this build)

1. **Merge** both branches (`ops114-automation` in hermes-agent and in
   scripts) after review.
2. **Register the two cron jobs** using the exact commands in
   `scripts/team_duncan_ops114_cron_manifest.json`
   (`hermes cron create ... --no-agent --deliver local`).
3. **Restart Gateway** if the hermes-agent branch touched anything the
   running process needs reloaded (plugin code changes typically do).
   This build makes no claim about whether that restart already happened:
   assume it has not.
4. **Plaud webhook** (optional: reconciliation alone is a complete,
   working recovery path without this step):
   a. In Zapier, connect Plaud OAuth and build a Zap on the
      **"Transcript & Summary Ready"** trigger.
   b. Map the trigger's recording ID field to `plaud_recording_id` (or
      `recording_id`) in a custom POST to
      `plaud_webhook_receiver.py`'s configured host:port/path. No other
      field needs mapping: the receiver ignores everything else.
   c. Generate an HMAC secret. `webhook_auth.load_or_create_webhook_secret()`
      will create and persist one automatically at
      `<hermes_home>/plugin-data/team_duncan_contacts/plaud_webhook_hmac_secret`
      (0600) the first time the receiver starts: read that file to get
      the value Zapier's outgoing request must sign with (generic V2
      scheme, §5).
   d. Start `plaud_webhook_receiver.py` under a process supervisor and
      expose its route (reverse proxy / port-forward) to Zapier. This is a
      new, standalone process this build does not start.
5. **Verify** using the ops checks in §8 before trusting the new paths in
   production.

## 8. Verification after activation

- `cat ~/.hermes/plugin-data/team_duncan_contacts/automation_heartbeats/desk.json`
  should show `last_attempt_at`/`last_success_at` advancing every 15
  minutes, `last_outcome: "completed"`.
- Same for `plaud_reconcile.json` every 3 hours.
- A deliberate lock collision (start an interactive `confirm_call_log_ingest`
  while a scheduled tick is mid-run) should produce
  `automation_lock_active` on whichever side loses the race, with the
  token left unconsumed on the interactive side.
- Tail `~/.hermes/cron/ledger/<today>.jsonl` for `"OPS-114 Team Duncan
  Desk Ingestion"` / `"... Plaud Reconciliation"` Green/Amber entries.

## 9. Rollback

- **Cron jobs**: `hermes cron pause <job_id>` (or delete) for either/both
  jobs registered in step 2 above. The manual interactive tools are
  unaffected and continue to work.
- **Webhook**: stop the `plaud_webhook_receiver.py` process and/or turn off
  the Zap. The 3-hour reconciliation job remains authoritative and picks up
  anything the webhook would have caught, within its own cadence + 4-hour
  absence threshold.
- **Watchdog extension**: revert the appended lines in
  `cron_watchdog_cron.sh` (scripts repo) to stop the OPS-114 absence/
  stuck-owner check; the existing fleet watchdog and healthchecks.io ping
  are untouched either way.
- No live data is mutated by any of the above: every rollback action is a
  scheduling/process change, not a data change. Desk cursor and Plaud
  frontier positions are left exactly where they were; resuming later
  picks up from there, idempotently.
