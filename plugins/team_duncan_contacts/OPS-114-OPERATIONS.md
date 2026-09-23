# OPS-114: Team Duncan Desk/Plaud Automation (Operations)

Status as of this build (no-Zapier 15-minute architecture): **code and
tests only**. Nothing here is scheduled, registered, or activated. Every
item under "Activation" below is a separate, Clay-controlled step.

Final architecture: there is no Zapier integration and no public webhook
path in active production. Plaud is covered exclusively by a 15-minute
metadata reconciliation sweep (`plaud-reconcile` mode). The webhook
receiver and its HMAC/queue machinery remain in source, fully tested, and
dormant, gated by an explicit default-off config flag
(`plaud_webhook_enabled: false`). Re-enabling it later is a separate,
Clay-controlled decision -- see §7.

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
  the receiver process. **Dormant in this architecture** -- see §5a.
- An explicit, default-off config gate,
  `plugins.entries.team_duncan_contacts.settings.plaud_webhook_enabled`,
  that both the receiver's `main()` and `automation_runner`'s
  `plaud-webhook` mode consult and fail closed on before touching any
  socket, secret, queue, lock, registry, or GHL state. See §5a.
- Scripts-repo wrappers (`team_duncan_ops114_report.py`,
  `team_duncan_ops114_desk_cron.sh`, `team_duncan_ops114_plaud_reconcile_cron.sh`)
  that route through the existing `cron_report.py` / `cron_ledger.py`
  Green/Amber contract.
- A watchdog extension (`team_duncan_ops114_watchdog.py`), chained into the
  existing `cron_watchdog_cron.sh` the same way
  `hindsight_circuit_red_watchdog.py` is chained today.

## 1a. Pending-review alert path: shared Cron-Amber is production authority

A live run created nine actionable pending reviews with no Telegram
notification reaching Clay. Root cause: the production runner factory
(`_build_ingestion_runner_factory`) wired the per-row notifier to two
`_UnconfiguredNotifier` instances (always return `False`), while still
calling `Notifier.send` and `record_notification_attempt` on every genuine
pending-review insertion and running the legacy due-notification retry
loop -- recording real-looking `retry_scheduled`/`retries_exhausted`
history for sends that never had any chance of delivering.

The correction: `IngestionRunner` takes an explicit `defer_notifications`
flag (default `False`, preserving direct-construction/test behavior
exactly). The production factory now always passes
`defer_notifications=True` for both manual and automated construction. In
deferred mode, a genuine pending-review insertion never calls
`Notifier.send` or `record_notification_attempt`, and `run()` never drives
the due-retry loop -- `notification_state` is left honestly at
`not_notified` for every new row, rather than recording a false attempt.
The notifier classes and legacy direct-injection behavior are unchanged
and still fully exercised by isolated tests (`defer_notifications=False`
or omitted); nothing was deleted.

**Shared Cron-Amber is the actual production pending-review alert path.**
`automation_runner._run_desk` now also emits a bounded, sorted,
content-safe projection of the current unresolved pending-review set
(`build_ops114_pending_review_summary`, at most 50 items, ordered by
`occurred_at` then id) alongside the existing count fields. The
scripts-repo `team_duncan_ops114_report.py` wrapper classifies
`pending_review_count > 0` as Amber material (even with zero errors) and
routes that projection through `cron_report.py` / `cron_ledger.py`'s
existing shared Amber machinery: one first alert, one 24-hour reminder,
suppression while unchanged, one recovery when the count reaches zero.

Per-row fields (`notification_state`, `notification_attempts`,
`last_notified_at`, `next_retry_at`) remain in the schema and remain
readable (e.g. via `list_pending_call_reviews`) as an audit/debugging
surface, but **they are not delivery authority in automated production**.
Rows already marked `retries_exhausted` from before this correction are
evidence of the prior broken path and are left exactly as they were --
nothing rewrites them.

## 2. Present manual baseline (unchanged by this build)

- **Desk**: `prepare_call_log_ingest` issues a single-use 5-minute token
  (no source read), rejects cron/background context, and blocks if an
  earlier run is still awaiting acceptance. `confirm_call_log_ingest`
  consumes the token and runs ingestion, leaving the run
  `awaiting_acceptance`. `accept_call_log_ingest_run` clears that hold.
  As of the §1a correction, the production factory defers per-row
  notification on this path too (`defer_notifications=True`), so a
  confirmed run no longer sends or retries per-row notifications -- see
  §1a for the current alert authority.
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
| OPS-114 Team Duncan Plaud Reconciliation | every 15 min, offset (`7,22,37,52 * * * *`) | `team_duncan_ops114_plaud_reconcile_cron.sh` | 45 min |

See `scripts/team_duncan_ops114_cron_manifest.json` for the exact
`hermes cron create ... --no-agent` commands. `--no-agent` means the
script *is* the job: no LLM involved, matching the "no LLM" requirement
for these two schedules. This build does not run either command.

Plaud reconciliation runs at :07/:22/:37/:52 -- deliberately offset from
Desk's :00/:15/:30/:45 ticks by 7 minutes, giving expected latency 7.5
minutes and maximum latency 15 minutes between a Plaud summary becoming
ready and the next reconciliation tick picking it up.

45 minutes (both Desk and Plaud reconciliation) = three expected
15-minute cadences, chosen to survive one overlap skip and one delayed
tick.

The per-tick wrapper's own process exit code (what the cron scheduler
sees) is 0 for both a completed run and a skipped-lock run: a clean
live-owner lock collision is not an ingestion failure, so it must not
surface as one to Hermes's `--no-agent` cron path (which treats any
nonzero exit as failure). It is still routed to `cron_report.py` as Green
either way. Only a crashed or genuinely failed run exits 1.

### Steady-state cost of 15-minute Plaud reconciliation

- 96 reconciliation runs/day.
- Ordinarily one `list_files` metadata tool call per idle run (no matched
  recording to process).
- Measured successful process wall time: 3.46 seconds/run, about 5.54
  minutes aggregate process wall time/day across all 96 runs.
- Transcript pagination, summarization, and GHL note work remain
  event-driven per matched recording and do not multiply with empty
  sweeps -- an idle tick costs one metadata call, nothing more.

## 5. Plaud webhook: current truth

There is no proven native signed personal-data webhook from Plaud. The
only ever-evaluated primary trigger was Plaud's documented Zapier
**"Transcript & Summary Ready"** trigger, mapped to a normalized custom
POST and authenticated at `plaud_webhook_receiver.py` with HMAC (the same
generic V2 scheme, `X-Webhook-Signature-V2` / `X-Webhook-Timestamp`,
HMAC-SHA256 over `"<timestamp>.<body>"`, 300s replay tolerance, already
used by `gateway/platforms/webhook.py`, so this receiver's auth is
consistent with the rest of the fleet even though it is not a route on
that adapter). Do not claim a native Plaud signature anywhere downstream
of this doc.

The receiver trusts only the immutable `plaud_recording_id` from the
webhook body: title, transcript text, speaker names, and summary are
never read from it, even if present (`normalize_zapier_payload`). All
actual processing metadata comes from a sealed `fetch_by_identity`
re-fetch inside `PlaudSummaryRunner.process_one()`, the same
never-trust-the-push-payload discipline every other sealed re-fetch in
this plugin already uses.

Durable acceptance (when enabled): before the receiver returns HTTP 202,
the immutable recording ID is committed to a small SQLite queue
(`webhook_queue.py`) under Team Duncan plugin data, deduped by recording
ID. A 202 is a crash-survival promise, not just an in-memory one; if the
durable write itself fails, the receiver returns 503 instead, never 202.
Once queued, the event is dispatched to
`automation_runner.run(["plaud-webhook", ...])` on a background thread as
a best-effort fast path (sharing the same OPS-114 durable lock every
automated mode uses) and removed from the queue only once that run
actually completes. `drain_pending_events()`, called at this receiver's
own process startup, replays anything still pending from before a prior
crash of this process, so an event accepted just before a crash is never
silently lost.

### 5a. No-Zapier 15-minute architecture: the webhook path is dormant

Clay's OPS-114 decision replaces the Zapier/webhook path with the
15-minute Plaud reconciliation sweep as the sole active Plaud path. There
is no live Zapier integration, no exposed Funnel route, and no running
receiver process in this architecture.

The receiver and its HMAC/queue/dispatch machinery are **not deleted**:
they remain in source, fully tested (see
`tests/plugins/team_duncan_contacts/test_plaud_webhook_receiver.py` and
`test_plaud_webhook_enabled_flag.py`), and gated behind one explicit,
default-off config setting:

```yaml
plugins:
  entries:
    team_duncan_contacts:
      settings:
        plaud_webhook_enabled: false
```

Only the literal boolean `true` enables the webhook path. Missing config,
`null`, `false`, a malformed config file, or any non-boolean value
(including the string `"true"`) all leave it disabled -- there is no
separate environment variable and no second config parser; this reuses
the same `load_config()` loader and settings path `location_id` already
reads (see `is_plaud_webhook_enabled()` in `__init__.py`).

With the flag absent or false (the default and current state):

- `plaud_webhook_receiver.main()` returns before binding a socket,
  loading or creating the HMAC secret, opening the webhook queue, or
  draining pending events. It logs one content-free line and exits
  `EXIT_DISABLED` (78).
- `automation_runner.run(["plaud-webhook", ...])` returns before lock
  acquisition, registry/GHL construction, queue access, any Plaud MCP
  connection, transcript fetch, or state mutation. It emits one
  content-free `{"status": "disabled", "mode": "plaud-webhook"}` line and
  exits `EXIT_DISABLED` (78).
- `desk` and `plaud-reconcile` modes are entirely unaffected by this
  setting.

The 15-minute reconciliation job (§4) is authoritative recovery in this
architecture, not merely a fallback for a webhook that might later exist:
it scans by frontier position on every tick regardless of whether a
webhook was ever configured.

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

Every automated mode acquires the lock before touching `ingestion_state.db`
(the `plaud-webhook` mode only reaches this point at all when
`plaud_webhook_enabled` is true -- see §5a).
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

1. **Merge** both branches (`ops114-no-zapier` in hermes-agent and in
   scripts) after review.
2. **Register the two cron jobs** using the exact commands in
   `scripts/team_duncan_ops114_cron_manifest.json`
   (`hermes cron create ... --no-agent --deliver local`). This activates
   Desk ingestion (every 15 minutes) and Plaud reconciliation (every 15
   minutes, offset at :07/:22/:37/:52) -- the complete, sole live Plaud
   path in this architecture. No Zapier setup, no Funnel exposure, and no
   receiver process are part of this activation.
3. **Restart Gateway** if the hermes-agent branch touched anything the
   running process needs reloaded (plugin code changes typically do).
   This build makes no claim about whether that restart already happened:
   assume it has not.

That is the entire activation for the current architecture. The Plaud
webhook receiver is not started, and no Zap is created, as part of OPS-114.

### Re-enabling the Plaud webhook later

The webhook path is intentionally out of scope for this activation. Turning
it on later is a **separate, Clay-controlled decision** requiring all of
the following, not just flipping the config flag:

1. Explicit fresh authorization for that specific change.
2. Setting `plugins.entries.team_duncan_contacts.settings.plaud_webhook_enabled`
   to the literal boolean `true` (see §5a) -- and only after the remaining
   steps below, not before.
3. A supervised receiver process (a real process manager, not an ad hoc
   background shell job) with restart-on-crash and log capture.
4. An authenticated public route reaching the receiver (reverse proxy or
   tunnel with its own access control, not a bare open port) -- HMAC
   verification alone is not a substitute for controlling who can even
   reach the endpoint.
5. A security review of the exposed surface (the route, the process
   supervisor config, and the secret handling) before any real Zapier Zap
   is pointed at it.

Until all five are done and separately approved, the flag stays false and
the 15-minute reconciliation sweep remains the sole live Plaud path.

## 8. Verification after activation

- `cat ~/.hermes/plugin-data/team_duncan_contacts/automation_heartbeats/desk.json`
  should show `last_attempt_at`/`last_success_at` advancing every 15
  minutes, `last_outcome: "completed"`.
- Same for `plaud_reconcile.json`, also every 15 minutes (offset ticks).
- A deliberate lock collision (start an interactive `confirm_call_log_ingest`
  while a scheduled tick is mid-run) should produce
  `automation_lock_active` on whichever side loses the race, with the
  token left unconsumed on the interactive side.
- Tail `~/.hermes/cron/ledger/<today>.jsonl` for `"OPS-114 Team Duncan
  Desk Ingestion"` / `"... Plaud Reconciliation"` Green/Amber entries.
- `plaud_webhook.json` should not exist (or should show no recent
  `last_attempt_at`) as long as `plaud_webhook_enabled` stays false --
  its presence with a recent timestamp would indicate the flag was
  turned on somewhere.

## 9. Rollback

- **Cron jobs**: `hermes cron pause <job_id>` (or delete) for either/both
  jobs registered in step 2 above. The manual interactive tools are
  unaffected and continue to work.
- **Webhook**: already the default/current state -- the receiver process
  is not running and `plaud_webhook_enabled` is false, so there is nothing
  to roll back on this axis unless a later, separately authorized
  re-enablement (see §7) is itself being reverted, in which case: stop the
  receiver process, turn off the Zap, and set `plaud_webhook_enabled` back
  to `false` (or remove the setting).
- **Watchdog extension**: revert the appended lines in
  `cron_watchdog_cron.sh` (scripts repo) to stop the OPS-114 absence/
  stuck-owner check; the existing fleet watchdog and healthchecks.io ping
  are untouched either way.
- No live data is mutated by any of the above: every rollback action is a
  scheduling/process change, not a data change. Desk cursor and Plaud
  frontier positions are left exactly where they were; resuming later
  picks up from there, idempotently.
