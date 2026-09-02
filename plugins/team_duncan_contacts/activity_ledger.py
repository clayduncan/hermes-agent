"""Team Duncan contact activity ledger (OPS-74).

Append-only SQLite ledger for contact-sourced events. Events are admitted only
after the registry's resolve_event gate clears; pre-activation events require
an explicit per-event override grant approved by clay.

Raw handles are passed to resolve_event and immediately discarded; they never
appear in any column, log, return value, or export.

See spec.md §3–6 for full invariant set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sanitizer import sanitize_output

log = logging.getLogger(__name__)

TEAM_DUNCAN_LOCATION_ID = "abi5iDumIeysZCvWt99r"

_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS contact_events (
    event_id         TEXT NOT NULL PRIMARY KEY,
    contact_id       TEXT NOT NULL,
    location_id      TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'active',
    superseded_by    TEXT,
    override_id      TEXT,
    provenance_json  TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_ce_contact_occurred
    ON contact_events (contact_id, occurred_at, event_id);

CREATE TABLE IF NOT EXISTS approved_overrides (
    override_id      TEXT NOT NULL PRIMARY KEY,
    contact_id       TEXT NOT NULL,
    location_id      TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    approved_by      TEXT NOT NULL DEFAULT 'clay',
    approved_at      TEXT NOT NULL,
    approval_reason  TEXT NOT NULL,
    state            TEXT NOT NULL DEFAULT 'active',
    UNIQUE (contact_id, location_id, source, source_event_id)
);

CREATE TABLE IF NOT EXISTS override_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    override_id  TEXT NOT NULL REFERENCES approved_overrides(override_id),
    ts           TEXT NOT NULL,
    transition   TEXT NOT NULL,
    actor        TEXT NOT NULL
);
"""

_VALID_STATES = frozenset({"active", "superseded", "override_admitted"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_id(source: str, source_event_id: str) -> str:
    return hashlib.sha256(f"{source}|{source_event_id}".encode()).hexdigest()


def _override_id(contact_id: str, source: str, source_event_id: str) -> str:
    return hashlib.sha256(
        f"{contact_id}|{source}|{source_event_id}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RecordResult:
    outcome: str  # 'admitted' | 'override_admitted' | 'discarded'
    event_id: str | None = None
    decision: str | None = None  # resolve_event decision when discarded


@dataclass
class CorrectResult:
    outcome: str  # 'corrected' | 'noop'
    new_event_id: str | None = None
    old_event_id: str | None = None


@dataclass
class GrantResult:
    outcome: str  # 'created' | 'reactivated' | 'noop'
    override_id: str | None = None


@dataclass
class RevokeResult:
    outcome: str  # 'revoked' | 'not_found' | 'already_revoked'
    override_id: str | None = None


@dataclass
class EventRow:
    event_id: str
    contact_id: str
    location_id: str
    source: str
    source_event_id: str
    occurred_at: str
    ingested_at: str
    state: str
    superseded_by: str | None
    override_id: str | None
    provenance_json: str


@dataclass
class OverrideHistoryRow:
    override_id: str
    contact_id: str
    location_id: str
    source: str
    source_event_id: str
    approved_by: str
    approved_at: str
    approval_reason: str
    state: str
    history: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ActivityLedger
# ---------------------------------------------------------------------------


class ActivityLedger:
    """Append-only SQLite ledger for Team Duncan contact activity events.

    Args:
        db_path: Path to the activity.db file. Created on first open.
        registry: Object exposing resolve_event(raw_handle, event_ts) -> ResolveResult.
    """

    def __init__(self, db_path: Path, registry: Any) -> None:
        self._db_path = Path(db_path)
        self._registry = registry
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # --- Internal DB helpers ---

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_INIT_SQL)

    # --- Public API ---

    def record_event(
        self,
        source: str,
        source_event_id: str,
        raw_handle: str,
        event_ts: datetime,
        provenance: dict,
    ) -> RecordResult:
        """Resolve handle, then persist if the ingest contract allows."""
        result = self._registry.resolve_event(raw_handle, event_ts)
        # raw_handle is no longer referenced after this point
        raw_handle = None  # noqa: F841 - explicit discard before any DB write

        decision = result.decision

        if decision == "allow":
            contact_id = result.ghl_contact_id
            location_id = result.location_id
            if location_id != TEAM_DUNCAN_LOCATION_ID:
                return RecordResult(outcome="discarded", decision="wrong_location")

            eid = _event_id(source, source_event_id)
            safe_provenance = json.dumps(
                sanitize_output(provenance) if isinstance(provenance, dict) else {}
            )
            ingested_at = _utc_now_iso()

            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO contact_events
                           (event_id, contact_id, location_id, source,
                            source_event_id, occurred_at, ingested_at,
                            state, provenance_json)
                           VALUES (?,?,?,?,?,?,?,'active',?)""",
                        (
                            eid, contact_id, location_id, source,
                            source_event_id,
                            event_ts.isoformat() if isinstance(event_ts, datetime)
                            else event_ts,
                            ingested_at, safe_provenance,
                        ),
                    )
            return RecordResult(outcome="admitted", event_id=eid, decision=decision)

        if decision == "deny_pre_activation":
            contact_id = result.ghl_contact_id
            location_id = result.location_id

            if location_id != TEAM_DUNCAN_LOCATION_ID:
                return RecordResult(outcome="discarded", decision="wrong_location")

            oid = _override_id(contact_id, source, source_event_id)

            with self._lock:
                with self._connect() as conn:
                    row = conn.execute(
                        """SELECT override_id FROM approved_overrides
                           WHERE override_id=? AND state='active'""",
                        (oid,),
                    ).fetchone()

                    if row is None:
                        return RecordResult(
                            outcome="discarded", decision=decision
                        )

                    eid = _event_id(source, source_event_id)
                    safe_provenance = json.dumps(
                        sanitize_output(provenance) if isinstance(provenance, dict) else {}
                    )
                    ingested_at = _utc_now_iso()

                    conn.execute(
                        """INSERT OR IGNORE INTO contact_events
                           (event_id, contact_id, location_id, source,
                            source_event_id, occurred_at, ingested_at,
                            state, override_id, provenance_json)
                           VALUES (?,?,?,?,?,?,?,'override_admitted',?,?)""",
                        (
                            eid, contact_id, location_id, source,
                            source_event_id,
                            event_ts.isoformat() if isinstance(event_ts, datetime)
                            else event_ts,
                            ingested_at, oid, safe_provenance,
                        ),
                    )
            return RecordResult(
                outcome="override_admitted", event_id=eid, decision=decision
            )

        # All other decisions: discard with no row written
        return RecordResult(outcome="discarded", decision=decision)

    def correct_event(
        self,
        source: str,
        old_source_event_id: str,
        new_source_event_id: str,
        raw_handle: str,
        new_event_ts: datetime,
        new_provenance: dict,
    ) -> CorrectResult:
        """Insert a correction row and mark the old row superseded atomically."""
        result = self._registry.resolve_event(raw_handle, new_event_ts)
        raw_handle = None  # noqa: F841 - discard before any DB write

        if result.decision != "allow":
            return CorrectResult(outcome="discarded")

        contact_id = result.ghl_contact_id
        location_id = result.location_id

        if location_id != TEAM_DUNCAN_LOCATION_ID:
            return CorrectResult(outcome="discarded")

        old_eid = _event_id(source, old_source_event_id)
        new_eid = _event_id(source, new_source_event_id)
        safe_provenance = json.dumps(
            sanitize_output(new_provenance) if isinstance(new_provenance, dict) else {}
        )
        ingested_at = _utc_now_iso()

        with self._lock:
            with self._connect() as conn:
                # Idempotency: if new row already exists and old is superseded, no-op
                existing_new = conn.execute(
                    "SELECT event_id FROM contact_events WHERE event_id=?", (new_eid,)
                ).fetchone()
                existing_old = conn.execute(
                    "SELECT state FROM contact_events WHERE event_id=?", (old_eid,)
                ).fetchone()

                if (
                    existing_new is not None
                    and existing_old is not None
                    and existing_old["state"] == "superseded"
                ):
                    return CorrectResult(
                        outcome="noop", new_event_id=new_eid, old_event_id=old_eid
                    )

                # Atomic: insert new + mark old superseded in one transaction
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO contact_events
                           (event_id, contact_id, location_id, source,
                            source_event_id, occurred_at, ingested_at,
                            state, provenance_json)
                           VALUES (?,?,?,?,?,?,?,'active',?)""",
                        (
                            new_eid, contact_id, location_id, source,
                            new_source_event_id,
                            new_event_ts.isoformat()
                            if isinstance(new_event_ts, datetime)
                            else new_event_ts,
                            ingested_at, safe_provenance,
                        ),
                    )
                    conn.execute(
                        """UPDATE contact_events
                           SET state='superseded', superseded_by=?
                           WHERE event_id=? AND state != 'superseded'""",
                        (new_eid, old_eid),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return CorrectResult(
            outcome="corrected", new_event_id=new_eid, old_event_id=old_eid
        )

    def grant_override(
        self,
        contact_id: str,
        location_id: str,
        source: str,
        source_event_id: str,
        approval_reason: str,
    ) -> GrantResult:
        """Create an event-scoped grant in approved_overrides.

        Not registered as an agent tool. Internal Python API only.
        """
        if location_id != TEAM_DUNCAN_LOCATION_ID:
            raise ValueError(
                f"location_id {location_id!r} does not match Team Duncan location. "
                "Override grants are restricted to Team Duncan contacts."
            )

        oid = _override_id(contact_id, source, source_event_id)
        safe_reason = str(sanitize_output(approval_reason))
        approved_at = _utc_now_iso()

        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT state FROM approved_overrides WHERE override_id=?", (oid,)
                ).fetchone()

                if existing is not None and existing["state"] == "active":
                    return GrantResult(outcome="noop", override_id=oid)

                conn.execute("BEGIN")
                try:
                    if existing is None:
                        conn.execute(
                            """INSERT INTO approved_overrides
                               (override_id, contact_id, location_id, source,
                                source_event_id, approved_by, approved_at,
                                approval_reason, state)
                               VALUES (?,?,?,?,?,'clay',?,?,'active')""",
                            (
                                oid, contact_id, location_id, source,
                                source_event_id, approved_at, safe_reason,
                            ),
                        )
                    else:
                        # Re-activate a revoked grant
                        conn.execute(
                            """UPDATE approved_overrides SET state='active',
                               approved_at=?, approval_reason=?
                               WHERE override_id=?""",
                            (approved_at, safe_reason, oid),
                        )

                    conn.execute(
                        """INSERT INTO override_history
                           (override_id, ts, transition, actor)
                           VALUES (?,?,'approved','clay')""",
                        (oid, approved_at),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        outcome = "reactivated" if existing is not None else "created"
        return GrantResult(outcome=outcome, override_id=oid)

    def revoke_override(
        self,
        contact_id: str,
        source: str,
        source_event_id: str,
    ) -> RevokeResult:
        """Set the override to state='revoked' and append a history row.

        Not registered as an agent tool. Internal Python API only.
        Events already admitted under this grant are not modified.
        """
        oid = _override_id(contact_id, source, source_event_id)
        ts = _utc_now_iso()

        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT state FROM approved_overrides WHERE override_id=?", (oid,)
                ).fetchone()

                if existing is None:
                    return RevokeResult(outcome="not_found", override_id=oid)

                if existing["state"] == "revoked":
                    return RevokeResult(outcome="already_revoked", override_id=oid)

                conn.execute("BEGIN")
                try:
                    conn.execute(
                        "UPDATE approved_overrides SET state='revoked' WHERE override_id=?",
                        (oid,),
                    )
                    conn.execute(
                        """INSERT INTO override_history
                           (override_id, ts, transition, actor)
                           VALUES (?,?,'revoked','clay')""",
                        (oid, ts),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return RevokeResult(outcome="revoked", override_id=oid)

    def query_events(
        self,
        contact_id: str,
        limit: int = 100,
        after_occurred_at: str | None = None,
    ) -> list[EventRow]:
        """Return active and override_admitted events, ordered chronologically."""
        with self._connect() as conn:
            if after_occurred_at is not None:
                rows = conn.execute(
                    """SELECT * FROM contact_events
                       WHERE contact_id=?
                         AND state IN ('active','override_admitted')
                         AND occurred_at > ?
                       ORDER BY occurred_at ASC, event_id ASC
                       LIMIT ?""",
                    (contact_id, after_occurred_at, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM contact_events
                       WHERE contact_id=?
                         AND state IN ('active','override_admitted')
                       ORDER BY occurred_at ASC, event_id ASC
                       LIMIT ?""",
                    (contact_id, limit),
                ).fetchall()
        return [_row_to_event(r) for r in rows]

    def query_full_history(self, contact_id: str) -> list[EventRow]:
        """Return all rows including superseded, ordered chronologically."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM contact_events
                   WHERE contact_id=?
                   ORDER BY occurred_at ASC, event_id ASC""",
                (contact_id,),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def query_override_history(self, contact_id: str) -> list[OverrideHistoryRow]:
        """Return all override records and their transition history for contact_id."""
        with self._connect() as conn:
            grants = conn.execute(
                """SELECT * FROM approved_overrides
                   WHERE contact_id=?
                   ORDER BY approved_at ASC""",
                (contact_id,),
            ).fetchall()

            result = []
            for g in grants:
                history_rows = conn.execute(
                    """SELECT ts, transition, actor FROM override_history
                       WHERE override_id=?
                       ORDER BY ts ASC""",
                    (g["override_id"],),
                ).fetchall()
                result.append(
                    OverrideHistoryRow(
                        override_id=g["override_id"],
                        contact_id=g["contact_id"],
                        location_id=g["location_id"],
                        source=g["source"],
                        source_event_id=g["source_event_id"],
                        approved_by=g["approved_by"],
                        approved_at=g["approved_at"],
                        approval_reason=g["approval_reason"],
                        state=g["state"],
                        history=[dict(r) for r in history_rows],
                    )
                )
        return result

    def export_ledger(self) -> dict:
        """Return all rows from all three tables as a serializable dict."""
        with self._connect() as conn:
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM contact_events ORDER BY occurred_at ASC, event_id ASC"
            ).fetchall()]
            overrides = [dict(r) for r in conn.execute(
                "SELECT * FROM approved_overrides ORDER BY approved_at ASC"
            ).fetchall()]
            history = [dict(r) for r in conn.execute(
                "SELECT * FROM override_history ORDER BY id ASC"
            ).fetchall()]

        return {
            "contact_events": events,
            "approved_overrides": overrides,
            "override_history": history,
        }

    def backup(self, dest_path: Path) -> None:
        """SQLite online backup to dest_path.

        Enforces: dest_path != live DB path, dest_path must not already exist.
        WAL-safe; works with open connections.
        """
        dest_path = Path(dest_path)
        if dest_path.resolve() == self._db_path.resolve():
            raise ValueError(
                "dest_path must not be the live DB path. "
                f"Refused: {dest_path}"
            )
        if dest_path.exists():
            raise ValueError(
                "dest_path already exists; backup requires a new empty destination. "
                f"Refused: {dest_path}"
            )

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with self._connect() as src_conn:
            with sqlite3.connect(dest_path) as dst_conn:
                src_conn.backup(dst_conn)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        event_id=row["event_id"],
        contact_id=row["contact_id"],
        location_id=row["location_id"],
        source=row["source"],
        source_event_id=row["source_event_id"],
        occurred_at=row["occurred_at"],
        ingested_at=row["ingested_at"],
        state=row["state"],
        superseded_by=row["superseded_by"],
        override_id=row["override_id"],
        provenance_json=row["provenance_json"],
    )
