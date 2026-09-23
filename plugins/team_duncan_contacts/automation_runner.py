"""OPS-114: the dedicated non-interactive automation entry point for Team
Duncan Desk ingestion and Plaud summary processing.

Never registered as an agent tool (no ``ctx.register_tool`` call anywhere in
this module), never imports ``gateway.session_context``, and never calls
``prepare_call_log_ingest`` / ``confirm_call_log_ingest`` /
``accept_call_log_ingest_run`` / ``prepare_plaud_summary_run`` /
``confirm_plaud_summary_run``. It calls straight into the same
``IngestionRunner`` / ``PlaudSummaryRunner`` seams those interactive tools
use, via the same factory functions and the same
``build_registry_and_reader`` construction path -- so this entry point and
the interactive plugin registration can never drift apart on config
binding, GHL scope, or startup validation.

Invocation (from a scripts-repo cron wrapper, never from this process's own
``if __name__`` block in production):

    python3 -m plugins.team_duncan_contacts.automation_runner desk
    python3 -m plugins.team_duncan_contacts.automation_runner plaud-reconcile
    python3 -m plugins.team_duncan_contacts.automation_runner plaud-webhook \\
        --plaud-recording-id <immutable Plaud recording ID>

Acquires the shared OPS-114 durable lock (process_lock.py) before touching
any state; a live-owner collision is a clean ``skipped_lock`` exit, never an
exception. Emits exactly one line of content-free JSON to stdout: counts,
status class, safe error class (an exception's type name, never its
message), lock result, and (via the heartbeat store) run timestamps. Never
prints a phone number, transcript, title, contact record, or secret.

Source-local isolation: a Desk failure never touches Plaud state and vice
versa (they are entirely separate invocations of this module). Within a
single Plaud/Desk run, `IngestionRunner`/`PlaudSummaryRunner` already never
advance a cursor/frontier past a failed record -- this module changes
nothing about that discipline, it only decides *whether* to call `run()`
at all this tick.

OPS-114 no-Zapier architecture: ``plaud-webhook`` mode fails closed (exits
EXIT_DISABLED, no lock/registry/queue/state work at all) unless
``plaud_webhook_enabled`` is the literal boolean true in config.yaml --
see ``is_plaud_webhook_enabled`` in this package's ``__init__.py``. The
default/current architecture is 15-minute Plaud reconciliation
(``plaud-reconcile``) only; ``desk`` and ``plaud-reconcile`` are entirely
unaffected by this flag.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

EXIT_COMPLETED = 0
EXIT_FAILED = 1
EXIT_SKIPPED_LOCK = 75  # sysexits.h EX_TEMPFAIL: transient, safe to retry next tick
EXIT_DISABLED = 78  # sysexits.h EX_CONFIG: plaud-webhook mode, not enabled by config

MODE_DESK = "desk"
MODE_PLAUD_RECONCILE = "plaud-reconcile"
MODE_PLAUD_WEBHOOK = "plaud-webhook"

_MARKER_FOR_MODE = {
    MODE_DESK: "desk",
    MODE_PLAUD_RECONCILE: "plaud_reconcile",
    MODE_PLAUD_WEBHOOK: "plaud_webhook",
}

_LOCK_MODE_FOR_MODE = {
    MODE_DESK: "desk-automated",
    MODE_PLAUD_RECONCILE: "plaud-reconcile-automated",
    MODE_PLAUD_WEBHOOK: "plaud-webhook-automated",
}

# RunSummary.to_dict() also carries `note_references` (contact_id +
# contact_url per note) -- Clay-facing detail that is appropriate for the
# interactive confirm_call_log_ingest response but not for a content-free
# automation log line. Every other field is a count, an opaque run_id, a
# timestamp, or a safe class-like string.
_DESK_SAFE_COUNT_KEYS = (
    "run_id",
    "admitted",
    "override_admitted",
    "discarded",
    "pending_review",
    "errors",
    "critical_errors",
    "critical_error_reasons",
    "pending_review_count",
    "oldest_pending_at",
    "failed_review_count",
    "oldest_failed_at",
    "notes_created",
    "notes_recovered",
    "note_errors",
)


def _sanitize_desk_counts(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw.get(key) for key in _DESK_SAFE_COUNT_KEYS}


def _has_processing_errors(counts: dict[str, Any]) -> bool:
    """True if the run's own counts report any per-record error, even
    though the run itself completed without raising. A Desk or Plaud run
    that finishes this way must not be recorded or reported as a clean
    success -- see run()'s completed/failed split below."""
    return bool(counts.get("errors") or counts.get("critical_errors"))


class RegistryUnavailableError(RuntimeError):
    """build_registry_and_reader could not construct a registry/GHL reader
    (missing config, failed startup validation, or a GHL scope failure)."""


def _run_desk(hermes_home: Path, registry: Any) -> dict[str, Any]:
    from . import _build_ingestion_runner_factory
    from .ingestion_state_db import build_ops114_pending_review_summary

    runner, state_db = _build_ingestion_runner_factory(hermes_home, registry)()
    summary = runner.run(token=None)
    counts = _sanitize_desk_counts(summary.to_dict())
    # OPS-114: production ingestion no longer sends any per-row Telegram/
    # email notification (defer_notifications=True in the production
    # factory) -- this bounded, content-safe projection is what lets the
    # scripts-repo report wrapper hand the shared cron Amber router one
    # actionable incident describing the current unresolved set.
    counts["pending_reviews"] = build_ops114_pending_review_summary(state_db)
    return counts


def _run_plaud_reconcile(hermes_home: Path, registry: Any, ghl_reader: Any) -> dict[str, Any]:
    from . import _build_plaud_summary_runner_factory

    runner, _state_db = _build_plaud_summary_runner_factory(hermes_home, registry, ghl_reader)()
    try:
        summary = runner.run(token=None)
        return summary.to_dict()
    finally:
        runner.close()


def _run_plaud_webhook(
    hermes_home: Path, registry: Any, ghl_reader: Any, plaud_recording_id: str
) -> dict[str, Any]:
    from . import _build_plaud_summary_runner_factory

    runner, _state_db = _build_plaud_summary_runner_factory(hermes_home, registry, ghl_reader)()
    try:
        summary = runner.process_one(plaud_recording_id)
        return summary.to_dict()
    finally:
        runner.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="team_duncan_automation",
        description="OPS-114 non-interactive Team Duncan Desk/Plaud automation runner.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser(MODE_DESK, help="Run the Desk call-log ingestion runner.")
    sub.add_parser(MODE_PLAUD_RECONCILE, help="Run the Plaud reconciliation scan.")
    webhook_parser = sub.add_parser(
        MODE_PLAUD_WEBHOOK, help="Process one Plaud transcript-ready webhook event."
    )
    webhook_parser.add_argument(
        "--plaud-recording-id",
        required=True,
        help="The immutable Plaud recording ID from the normalized webhook event.",
    )
    return parser


def run(
    argv: list[str] | None = None,
    *,
    hermes_home: Path | None = None,
) -> int:
    """Parse argv, acquire the shared lock, dispatch to the requested mode,
    and record a heartbeat. Returns the process exit code; never raises.

    ``plaud-webhook`` fails closed before lock acquisition, registry/GHL
    construction, queue access, Plaud MCP connection, transcript fetch, or
    any state mutation if ``plaud_webhook_enabled`` is not the literal
    boolean true (see ``is_plaud_webhook_enabled`` in this package's
    ``__init__.py``); ``desk`` and ``plaud-reconcile`` are unaffected by
    this setting."""
    from . import build_registry_and_reader, is_plaud_webhook_enabled

    args = _build_arg_parser().parse_args(argv)
    mode: str = args.mode
    marker = _MARKER_FOR_MODE[mode]

    if mode == MODE_PLAUD_WEBHOOK and not is_plaud_webhook_enabled():
        _emit({"status": "disabled", "mode": mode})
        return EXIT_DISABLED

    from hermes_constants import get_hermes_home

    from .heartbeat import (
        OUTCOME_COMPLETED,
        OUTCOME_FAILED,
        OUTCOME_SKIPPED_LOCK,
        HeartbeatStore,
    )
    from .process_lock import TeamDuncanLock

    resolved_home = Path(hermes_home) if hermes_home is not None else Path(get_hermes_home())
    data_dir = resolved_home / "plugin-data" / "team_duncan_contacts"
    data_dir.mkdir(parents=True, exist_ok=True)
    heartbeats = HeartbeatStore(data_dir)

    lock = TeamDuncanLock(hermes_home=resolved_home)
    lock_result = lock.acquire(mode=_LOCK_MODE_FOR_MODE[mode])
    if not lock_result.acquired:
        heartbeats.record(
            marker, outcome=OUTCOME_SKIPPED_LOCK, mode=mode, owner_pid=lock_result.owner_pid,
        )
        _emit({"status": "skipped_lock", "mode": mode, "locked_by_pid": lock_result.owner_pid})
        return EXIT_SKIPPED_LOCK

    try:
        registry, ghl_reader, _location_id = build_registry_and_reader(resolved_home)
        if registry is None:
            raise RegistryUnavailableError(
                "build_registry_and_reader returned no registry (config, "
                "startup validation, or GHL reader build failed)."
            )

        if mode == MODE_DESK:
            counts = _run_desk(resolved_home, registry)
        elif mode == MODE_PLAUD_RECONCILE:
            counts = _run_plaud_reconcile(resolved_home, registry, ghl_reader)
        else:
            counts = _run_plaud_webhook(
                resolved_home, registry, ghl_reader, args.plaud_recording_id
            )

        if _has_processing_errors(counts):
            heartbeats.record(marker, outcome=OUTCOME_FAILED, mode=mode, counts=counts)
            _emit({"status": "failed", "mode": mode, "counts": counts})
            return EXIT_FAILED

        heartbeats.record(marker, outcome=OUTCOME_COMPLETED, mode=mode, counts=counts)
        _emit({"status": "completed", "mode": mode, "counts": counts})
        return EXIT_COMPLETED
    except Exception as exc:
        error_class = type(exc).__name__
        log.error("team_duncan automation mode=%s failed [%s]", mode, error_class)
        heartbeats.record(marker, outcome=OUTCOME_FAILED, mode=mode, error_class=error_class)
        _emit({"status": "failed", "mode": mode, "error_class": error_class})
        return EXIT_FAILED
    finally:
        lock.release()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    return run(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
