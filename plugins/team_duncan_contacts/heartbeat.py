"""OPS-114 content-free heartbeat markers for automated Team Duncan runs.

One atomic JSON file per marker (``desk``, ``plaud_reconcile``,
``plaud_webhook``) under
``<hermes_home>/plugin-data/team_duncan_contacts/automation_heartbeats/``.
Never holds a phone number, transcript text, title, contact record, or
secret -- only timestamps, a closed outcome enum, a mode label, an
optional numeric-counts dict (the runner's own already-sanitized
``RunSummary``/``PlaudSummaryRunSummary``), and a safe error class string
(an exception's *type name*, never its message).

Outcome semantics (Green/Amber/Red routing reads these, never writes them):
  - ``skipped_lock``: a live owner held the lock. ``last_attempt_at`` moves;
    ``last_completed_at``/``last_success_at`` do not.
  - ``failed``: the run attempted and raised. ``last_attempt_at`` and
    ``last_completed_at`` move; ``last_success_at`` does not.
  - ``completed``: a clean run. All three timestamps move.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOME_COMPLETED = "completed"
OUTCOME_SKIPPED_LOCK = "skipped_lock"
OUTCOME_FAILED = "failed"
KNOWN_OUTCOMES = frozenset({OUTCOME_COMPLETED, OUTCOME_SKIPPED_LOCK, OUTCOME_FAILED})

MARKER_DESK = "desk"
MARKER_PLAUD_RECONCILE = "plaud_reconcile"
MARKER_PLAUD_WEBHOOK = "plaud_webhook"
KNOWN_MARKERS = frozenset({MARKER_DESK, MARKER_PLAUD_RECONCILE, MARKER_PLAUD_WEBHOOK})


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class HeartbeatStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir) / "automation_heartbeats"

    def _path(self, marker: str) -> Path:
        return self._dir / f"{marker}.json"

    def read(self, marker: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(marker).read_text())
        except (OSError, ValueError):
            return None

    def record(
        self,
        marker: str,
        *,
        outcome: str,
        mode: str,
        counts: dict[str, Any] | None = None,
        error_class: str | None = None,
        owner_pid: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if outcome not in KNOWN_OUTCOMES:
            raise ValueError(f"Unknown heartbeat outcome: {outcome!r}")
        moment = now or datetime.now(timezone.utc)

        state = self.read(marker) or {
            "last_attempt_at": None,
            "last_completed_at": None,
            "last_success_at": None,
        }
        state["last_attempt_at"] = _iso(moment)
        state["last_outcome"] = outcome
        state["mode"] = mode
        state["active_owner_pid"] = owner_pid if outcome == OUTCOME_SKIPPED_LOCK else None
        state["counts"] = dict(counts) if counts else {}
        state["error_class"] = error_class

        if outcome in (OUTCOME_COMPLETED, OUTCOME_FAILED):
            state["last_completed_at"] = _iso(moment)
        if outcome == OUTCOME_COMPLETED:
            state["last_success_at"] = _iso(moment)

        self._write_atomic(marker, state)
        return state

    def _write_atomic(self, marker: str, state: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(marker)
        tmp_path = path.with_suffix(f".tmp{os.getpid()}")
        tmp_path.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp_path, path)


__all__ = [
    "HeartbeatStore",
    "OUTCOME_COMPLETED",
    "OUTCOME_SKIPPED_LOCK",
    "OUTCOME_FAILED",
    "KNOWN_OUTCOMES",
    "MARKER_DESK",
    "MARKER_PLAUD_RECONCILE",
    "MARKER_PLAUD_WEBHOOK",
    "KNOWN_MARKERS",
]
