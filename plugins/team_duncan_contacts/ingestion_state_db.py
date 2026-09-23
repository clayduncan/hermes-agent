"""OPS-18 durable operational state for call-log ingestion.

A dedicated stdlib SQLite WAL database, separate from the OPS-74 activity
ledger (`activity.db`). Holds everything the ingestion runner, the review
workflow, the manual-run gate, and the Pending Call Reviews surface need:
source cursors, the pending-review queue and its append-only history,
processed non-actionable outcomes, manual-run confirmation tokens, run
summaries, and per-action approval tokens.

No raw handle, transcript, summary, or mind-map field is ever a column here.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import os
import secrets
import sqlite3
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .sanitizer import sanitize_output

# --- Source identity (exact strings, do not rename) ---
SOURCE_PLAUD = "plaud"
SOURCE_DESK_CALL = "desk_call"
KNOWN_SOURCES = (SOURCE_PLAUD, SOURCE_DESK_CALL)

# --- Closed pending_review.status enum (14 values) ---
STATUS_PENDING_REVIEW = "pending_review"
STATUS_CONTACT_SELECTED = "contact_selected"
STATUS_CONTACT_CREATION_PENDING = "contact_creation_pending"
STATUS_CONTACT_CREATED = "contact_created"
STATUS_AWAITING_ACTIVATION_CONFIRMATION = "awaiting_activation_confirmation"
STATUS_CONTACT_ACTIVATED = "contact_activated"
STATUS_GRANT_ISSUED = "grant_issued"
STATUS_REPLAYED = "replayed"
STATUS_COMPLETE = "complete"
STATUS_DISMISSED = "dismissed"
STATUS_DUPLICATE_RESOLUTION_REQUIRED = "duplicate_resolution_required"
STATUS_SOURCE_MISSING = "source_missing"
STATUS_SOURCE_AMBIGUOUS = "source_ambiguous"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETE,
        STATUS_DISMISSED,
        STATUS_DUPLICATE_RESOLUTION_REQUIRED,
        STATUS_SOURCE_MISSING,
        STATUS_SOURCE_AMBIGUOUS,
        STATUS_FAILED,
    }
)

#: Valid forward transitions. `advance_pending_review_stage` rejects anything
#: not listed here; every terminal status maps to an empty set.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING_REVIEW: frozenset(
        {
            STATUS_CONTACT_SELECTED,
            STATUS_CONTACT_CREATION_PENDING,
            STATUS_GRANT_ISSUED,
            STATUS_DUPLICATE_RESOLUTION_REQUIRED,
            STATUS_DISMISSED,
            STATUS_SOURCE_MISSING,
            STATUS_SOURCE_AMBIGUOUS,
            STATUS_FAILED,
        }
    ),
    STATUS_CONTACT_SELECTED: frozenset(
        {STATUS_GRANT_ISSUED, STATUS_DISMISSED, STATUS_SOURCE_MISSING,
         STATUS_SOURCE_AMBIGUOUS, STATUS_FAILED}
    ),
    STATUS_CONTACT_CREATION_PENDING: frozenset(
        {STATUS_CONTACT_CREATED, STATUS_DISMISSED, STATUS_SOURCE_MISSING,
         STATUS_SOURCE_AMBIGUOUS, STATUS_FAILED}
    ),
    STATUS_CONTACT_CREATED: frozenset(
        {STATUS_AWAITING_ACTIVATION_CONFIRMATION, STATUS_DISMISSED, STATUS_FAILED}
    ),
    STATUS_AWAITING_ACTIVATION_CONFIRMATION: frozenset(
        {STATUS_CONTACT_ACTIVATED, STATUS_DISMISSED, STATUS_FAILED}
    ),
    STATUS_CONTACT_ACTIVATED: frozenset(
        {STATUS_GRANT_ISSUED, STATUS_DISMISSED, STATUS_FAILED}
    ),
    STATUS_GRANT_ISSUED: frozenset({STATUS_REPLAYED, STATUS_DISMISSED, STATUS_FAILED}),
    STATUS_REPLAYED: frozenset({STATUS_COMPLETE, STATUS_FAILED}),
    STATUS_COMPLETE: frozenset(),
    STATUS_DISMISSED: frozenset(),
    STATUS_DUPLICATE_RESOLUTION_REQUIRED: frozenset(),
    STATUS_SOURCE_MISSING: frozenset(),
    STATUS_SOURCE_AMBIGUOUS: frozenset(),
    STATUS_FAILED: frozenset(),
}

# --- Notification state ---
NOTIF_NOT_NOTIFIED = "not_notified"
NOTIF_NOTIFIED = "notified"
NOTIF_RETRY_SCHEDULED = "retry_scheduled"
NOTIF_RETRIES_EXHAUSTED = "retries_exhausted"

NOTIFICATION_MAX_ATTEMPTS = 3
_RETRY_1_DELAY = timedelta(minutes=15)
_RETRY_2_DELAY_FROM_INITIAL = timedelta(minutes=60)

# --- Outcomes that get a durable non-actionable record but no review row ---
OUTCOME_DENY_PAUSED = "deny_paused"
OUTCOME_DENY_RETIRED = "deny_retired"
OUTCOME_UNEXPECTED_DECISION = "unexpected_decision"

# --- OPS-18 GHL note mirror state (crash-safe idempotency, keyed by ledger event_id) ---
NOTE_MIRROR_STATUS_PENDING = "pending"
NOTE_MIRROR_STATUS_COMPLETE = "complete"

# --- OPS-110 Plaud summary state (match/transcript/summary tracking) ---
PLAUD_MATCH_STATUS_MATCHED = "matched"
PLAUD_MATCH_STATUS_UNMATCHED = "unmatched"
PLAUD_MATCH_STATUS_AMBIGUOUS = "ambiguous"

PLAUD_TRANSCRIPT_STATUS_NOT_FETCHED = "not_fetched"
PLAUD_TRANSCRIPT_STATUS_FETCHED = "fetched"
PLAUD_TRANSCRIPT_STATUS_FAILED = "failed"

PLAUD_SUMMARY_STATUS_NOT_STARTED = "not_started"
PLAUD_SUMMARY_STATUS_COMPLETE = "complete"
PLAUD_SUMMARY_STATUS_FAILED = "failed"

# --- Manual-run / action-approval token TTLs (mirrors registry.py precedent) ---
CONFIRMATION_TTL_SECONDS = 300
ACTION_APPROVAL_TTL_SECONDS = 300

ACTION_CREATE_CONTACT = "create_contact"
ACTION_GRANT_OVERRIDE = "grant_override"
KNOWN_ACTIONS = (ACTION_CREATE_CONTACT, ACTION_GRANT_OVERRIDE)

_IDENTITY_KEY_FILE_NAME = "ingestion_identity_key"
_IDENTITY_KEY_LENGTH = 32

_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS source_cursors (
    source      TEXT NOT NULL PRIMARY KEY,
    checkpoint  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_review (
    id                     TEXT NOT NULL PRIMARY KEY,
    source                 TEXT NOT NULL,
    source_event_id        TEXT NOT NULL,
    decision               TEXT NOT NULL,
    match_outcome          TEXT,
    occurred_at            TEXT NOT NULL,
    duration_s             INTEGER,
    direction              TEXT,
    answered               INTEGER,
    status                 TEXT NOT NULL DEFAULT 'pending_review',
    resolved_contact_id    TEXT,
    idempotency_key        TEXT,
    failure_stage          TEXT,
    failure_detail         TEXT,
    notification_state     TEXT NOT NULL DEFAULT 'not_notified',
    notification_attempts  INTEGER NOT NULL DEFAULT 0,
    last_notified_at       TEXT,
    next_retry_at          TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    display_name           TEXT,
    masked_labels          TEXT,
    UNIQUE (source, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_review_status_created
    ON pending_review (status, created_at, id);

CREATE TABLE IF NOT EXISTS pending_review_history (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_review_id  TEXT NOT NULL,
    ts                 TEXT NOT NULL,
    from_status        TEXT,
    to_status          TEXT NOT NULL,
    actor              TEXT NOT NULL DEFAULT 'clay',
    detail             TEXT
);

CREATE TABLE IF NOT EXISTS processed_outcomes (
    id               TEXT NOT NULL PRIMARY KEY,
    source           TEXT NOT NULL,
    source_event_id  TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    processed_at     TEXT NOT NULL,
    UNIQUE (source, source_event_id)
);

CREATE TABLE IF NOT EXISTS ingestion_confirmations (
    token_hash  TEXT NOT NULL PRIMARY KEY,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    state       TEXT NOT NULL DEFAULT 'issued'
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id             TEXT NOT NULL PRIMARY KEY,
    started_at         TEXT NOT NULL,
    completed_at       TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'awaiting_acceptance',
    admitted           INTEGER NOT NULL,
    override_admitted  INTEGER NOT NULL,
    discarded          INTEGER NOT NULL,
    pending_review     INTEGER NOT NULL,
    errors             INTEGER NOT NULL,
    critical_errors    INTEGER NOT NULL,
    accepted_at        TEXT
);

CREATE TABLE IF NOT EXISTS action_approval_tokens (
    token_hash         TEXT NOT NULL PRIMARY KEY,
    pending_review_id  TEXT NOT NULL,
    action             TEXT NOT NULL,
    issued_at          TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    used_at            TEXT,
    state              TEXT NOT NULL DEFAULT 'issued'
);

CREATE TABLE IF NOT EXISTS note_mirror (
    event_id         TEXT NOT NULL PRIMARY KEY,
    contact_id       TEXT NOT NULL,
    note_id          TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    content_hash     TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_attempt_at  TEXT
);

CREATE TABLE IF NOT EXISTS plaud_summary_state (
    id                     TEXT NOT NULL PRIMARY KEY,
    plaud_recording_id     TEXT NOT NULL,
    desk_source_event_id   TEXT,
    contact_id             TEXT,
    match_status           TEXT NOT NULL,
    transcript_status      TEXT NOT NULL DEFAULT 'not_fetched',
    summary_status         TEXT NOT NULL DEFAULT 'not_started',
    claude_output_hash     TEXT,
    note_id                TEXT,
    error_class            TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (plaud_recording_id)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def pending_review_id(source: str, source_event_id: str) -> str:
    return hashlib.sha256(f"{source}|{source_event_id}".encode()).hexdigest()


def processed_outcome_id(source: str, source_event_id: str) -> str:
    return hashlib.sha256(f"{source}|{source_event_id}".encode()).hexdigest()


def plaud_summary_state_id(plaud_recording_id: str) -> str:
    return hashlib.sha256(f"plaud_summary|{plaud_recording_id}".encode()).hexdigest()


def creation_idempotency_key(source: str, source_event_id: str) -> str:
    return hashlib.sha256(
        f"team_duncan|create_contact|{source}|{source_event_id}".encode()
    ).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_or_create_identity_key(data_dir: Path) -> bytes:
    """Load (or create) the dedicated key used to derive Desk source_event_ids.

    Kept separate from registry.py's contact-matching HMAC key: independent
    key material for an independent purpose. 0600 permissions, never emitted.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / _IDENTITY_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) != _IDENTITY_KEY_LENGTH:
            raise ValueError(
                f"Ingestion identity key file {key_path} has unexpected length "
                f"{len(key)} (expected {_IDENTITY_KEY_LENGTH})."
            )
        return key
    key = secrets.token_bytes(_IDENTITY_KEY_LENGTH)
    key_path.write_bytes(key)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    return key


# ---------------------------------------------------------------------------
# Result / row types
# ---------------------------------------------------------------------------


@dataclass
class InsertPendingReviewResult:
    genuine_insert: bool
    pending_review_id: str


@dataclass
class PendingReviewRow:
    id: str
    source: str
    source_event_id: str
    decision: str
    match_outcome: str | None
    occurred_at: str
    duration_s: int | None
    direction: str | None
    answered: int | None
    status: str
    resolved_contact_id: str | None
    idempotency_key: str | None
    failure_stage: str | None
    failure_detail: str | None
    notification_state: str
    notification_attempts: int
    last_notified_at: str | None
    next_retry_at: str | None
    created_at: str
    updated_at: str
    display_name: str | None
    masked_labels: dict[str, Any] = field(default_factory=dict)


def _row_to_pending_review(row: sqlite3.Row) -> PendingReviewRow:
    masked = row["masked_labels"]
    try:
        masked_labels = json.loads(masked) if masked else {}
    except (TypeError, ValueError):
        masked_labels = {}
    return PendingReviewRow(
        id=row["id"],
        source=row["source"],
        source_event_id=row["source_event_id"],
        decision=row["decision"],
        match_outcome=row["match_outcome"],
        occurred_at=row["occurred_at"],
        duration_s=row["duration_s"],
        direction=row["direction"],
        answered=row["answered"],
        status=row["status"],
        resolved_contact_id=row["resolved_contact_id"],
        idempotency_key=row["idempotency_key"],
        failure_stage=row["failure_stage"],
        failure_detail=row["failure_detail"],
        notification_state=row["notification_state"],
        notification_attempts=row["notification_attempts"],
        last_notified_at=row["last_notified_at"],
        next_retry_at=row["next_retry_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        display_name=row["display_name"],
        masked_labels=masked_labels,
    )


@dataclass
class NoteMirrorRow:
    event_id: str
    contact_id: str
    note_id: str | None
    status: str
    content_hash: str | None
    attempts: int
    created_at: str
    updated_at: str
    last_attempt_at: str | None


def _row_to_note_mirror(row: sqlite3.Row) -> NoteMirrorRow:
    return NoteMirrorRow(
        event_id=row["event_id"],
        contact_id=row["contact_id"],
        note_id=row["note_id"],
        status=row["status"],
        content_hash=row["content_hash"],
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_attempt_at=row["last_attempt_at"],
    )


@dataclass
class PlaudSummaryStateRow:
    id: str
    plaud_recording_id: str
    desk_source_event_id: str | None
    contact_id: str | None
    match_status: str
    transcript_status: str
    summary_status: str
    claude_output_hash: str | None
    note_id: str | None
    error_class: str | None
    created_at: str
    updated_at: str


def _row_to_plaud_summary_state(row: sqlite3.Row) -> PlaudSummaryStateRow:
    return PlaudSummaryStateRow(
        id=row["id"],
        plaud_recording_id=row["plaud_recording_id"],
        desk_source_event_id=row["desk_source_event_id"],
        contact_id=row["contact_id"],
        match_status=row["match_status"],
        transcript_status=row["transcript_status"],
        summary_status=row["summary_status"],
        claude_output_hash=row["claude_output_hash"],
        note_id=row["note_id"],
        error_class=row["error_class"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class InvalidTransitionError(ValueError):
    pass


class TokenError(ValueError):
    pass


# ---------------------------------------------------------------------------
# IngestionStateDb
# ---------------------------------------------------------------------------


class IngestionStateDb:
    """Durable OPS-18 operational state, separate from the activity ledger."""

    def __init__(self, db_path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._db_path = Path(db_path)
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_INIT_SQL)

    def _now_iso(self) -> str:
        return _iso(self._clock())

    # --- source_cursors -----------------------------------------------------

    def get_cursor(self, source: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT checkpoint FROM source_cursors WHERE source=?", (source,)
            ).fetchone()
        return row["checkpoint"] if row else None

    def set_cursor(self, source: str, checkpoint: str) -> None:
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO source_cursors (source, checkpoint, updated_at)
                       VALUES (?,?,?)
                       ON CONFLICT(source) DO UPDATE SET
                           checkpoint=excluded.checkpoint,
                           updated_at=excluded.updated_at""",
                    (source, checkpoint, now),
                )

    # --- pending_review -------------------------------------------------------

    def insert_pending_review(
        self,
        *,
        source: str,
        source_event_id: str,
        decision: str,
        match_outcome: str | None,
        occurred_at: str,
        duration_s: int | None,
        direction: str | None,
        answered: int | None,
        status: str,
        display_name: str | None = None,
        masked_labels: dict[str, Any] | None = None,
    ) -> InsertPendingReviewResult:
        """INSERT OR IGNORE; genuine_insert reflects sqlite's own changes()."""
        rid = pending_review_id(source, source_event_id)
        now = self._now_iso()
        safe_display_name = sanitize_output(display_name) if display_name else None
        safe_masked = json.dumps(sanitize_output(masked_labels or {}))
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO pending_review
                           (id, source, source_event_id, decision, match_outcome,
                            occurred_at, duration_s, direction, answered, status,
                            notification_state, notification_attempts,
                            created_at, updated_at, display_name, masked_labels)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                        (
                            rid, source, source_event_id, decision, match_outcome,
                            occurred_at, duration_s, direction, answered, status,
                            NOTIF_NOT_NOTIFIED, now, now, safe_display_name, safe_masked,
                        ),
                    )
                    changed = conn.execute("SELECT changes()").fetchone()[0]
                    if changed == 1:
                        conn.execute(
                            """INSERT INTO pending_review_history
                               (pending_review_id, ts, from_status, to_status, actor, detail)
                               VALUES (?,?,NULL,?,?,?)""",
                            (rid, now, status, "system", "genuine_insert"),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return InsertPendingReviewResult(genuine_insert=changed == 1, pending_review_id=rid)

    def get_pending_review(self, pending_review_id_: str) -> PendingReviewRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_review WHERE id=?", (pending_review_id_,)
            ).fetchone()
        return _row_to_pending_review(row) if row else None

    def query_pending_review(
        self,
        *,
        status: str | None = None,
        notification_state: str | None = None,
        match_outcome: str | None = None,
        source: str | None = None,
        limit: int = 50,
        order: str = "oldest_first",
    ) -> list[PendingReviewRow]:
        """Bounded query; rejects an unbounded request (limit must be >0 and capped)."""
        if limit is None or limit <= 0:
            raise ValueError("limit must be a positive integer; unbounded queries are rejected.")
        limit = min(limit, 200)
        clauses = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if notification_state is not None:
            clauses.append("notification_state=?")
            params.append(notification_state)
        if match_outcome is not None:
            clauses.append("match_outcome=?")
            params.append(match_outcome)
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_sql = "ASC" if order == "oldest_first" else "DESC"
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM pending_review {where}
                    ORDER BY created_at {order_sql}, id {order_sql}
                    LIMIT ?""",
                (*params, limit),
            ).fetchall()
        return [_row_to_pending_review(r) for r in rows]

    def query_unresolved_pending_review(self, *, limit: int = 50) -> list[PendingReviewRow]:
        """Bounded query for every pending_review row not yet in a terminal
        status (the same set `count_unresolved_pending_review` counts),
        ordered by occurred_at then id for deterministic pagination --
        OPS-114's report projection depends on this exact, stable order."""
        if limit is None or limit <= 0:
            raise ValueError("limit must be a positive integer; unbounded queries are rejected.")
        limit = min(limit, 200)
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM pending_review
                    WHERE status NOT IN ({placeholders})
                    ORDER BY occurred_at ASC, id ASC
                    LIMIT ?""",
                (*TERMINAL_STATUSES, limit),
            ).fetchall()
        return [_row_to_pending_review(r) for r in rows]

    def count_unresolved_pending_review(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM pending_review
                   WHERE status NOT IN (?,?,?,?,?,?)""",
                (
                    STATUS_COMPLETE, STATUS_DISMISSED, STATUS_DUPLICATE_RESOLUTION_REQUIRED,
                    STATUS_SOURCE_MISSING, STATUS_SOURCE_AMBIGUOUS, STATUS_FAILED,
                ),
            ).fetchone()
        return row["n"]

    def oldest_pending_at(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT MIN(created_at) AS oldest FROM pending_review
                   WHERE status NOT IN (?,?,?,?,?,?)""",
                (
                    STATUS_COMPLETE, STATUS_DISMISSED, STATUS_DUPLICATE_RESOLUTION_REQUIRED,
                    STATUS_SOURCE_MISSING, STATUS_SOURCE_AMBIGUOUS, STATUS_FAILED,
                ),
            ).fetchone()
        return row["oldest"]

    def count_failed_pending_review(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pending_review WHERE status=?",
                (STATUS_FAILED,),
            ).fetchone()
        return row["n"]

    def oldest_failed_at(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(created_at) AS oldest FROM pending_review WHERE status=?",
                (STATUS_FAILED,),
            ).fetchone()
        return row["oldest"]

    def advance_pending_review_stage(
        self,
        pending_review_id_: str,
        new_status: str,
        *,
        actor: str = "clay",
        detail: str | None = None,
        resolved_contact_id: str | None = None,
        failure_stage: str | None = None,
        failure_detail: str | None = None,
    ) -> PendingReviewRow:
        """The sole writer of pending_review.status after initial insert.

        Validates the transition table; idempotent no-op if `new_status`
        already equals the current status (safe replay after a crash).
        Rejects any transition not listed in _TRANSITIONS.
        """
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM pending_review WHERE id=?", (pending_review_id_,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"pending_review {pending_review_id_!r} not found.")
                current = row["status"]

                if current == new_status:
                    return _row_to_pending_review(row)

                allowed = _TRANSITIONS.get(current, frozenset())
                if new_status not in allowed:
                    raise InvalidTransitionError(
                        f"Cannot transition pending_review {pending_review_id_!r} "
                        f"from {current!r} to {new_status!r}."
                    )

                now = self._now_iso()
                safe_detail = str(sanitize_output(detail)) if detail else None
                safe_failure_detail = (
                    str(sanitize_output(failure_detail)) if failure_detail else None
                )
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        """UPDATE pending_review
                           SET status=?, updated_at=?,
                               resolved_contact_id=COALESCE(?, resolved_contact_id),
                               failure_stage=COALESCE(?, failure_stage),
                               failure_detail=COALESCE(?, failure_detail)
                           WHERE id=?""",
                        (
                            new_status, now, resolved_contact_id, failure_stage,
                            safe_failure_detail, pending_review_id_,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO pending_review_history
                           (pending_review_id, ts, from_status, to_status, actor, detail)
                           VALUES (?,?,?,?,?,?)""",
                        (pending_review_id_, now, current, new_status, actor, safe_detail),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return self.get_pending_review(pending_review_id_)

    def record_annotation(
        self, pending_review_id_: str, detail: str, *, actor: str = "clay"
    ) -> None:
        """Append a history row with no status change -- e.g. to record a
        correction against an already-terminal row without violating the
        transition table (which forbids self-loops carrying new information)."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM pending_review WHERE id=?", (pending_review_id_,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"pending_review {pending_review_id_!r} not found.")
                now = self._now_iso()
                conn.execute(
                    """INSERT INTO pending_review_history
                       (pending_review_id, ts, from_status, to_status, actor, detail)
                       VALUES (?,?,?,?,?,?)""",
                    (pending_review_id_, now, row["status"], row["status"], actor,
                     str(sanitize_output(detail))),
                )

    def set_idempotency_key(self, pending_review_id_: str, idempotency_key: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pending_review SET idempotency_key=?, updated_at=? WHERE id=?",
                    (idempotency_key, self._now_iso(), pending_review_id_),
                )

    def get_history(self, pending_review_id_: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT ts, from_status, to_status, actor, detail
                   FROM pending_review_history
                   WHERE pending_review_id=? ORDER BY seq ASC""",
                (pending_review_id_,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- processed_outcomes ---------------------------------------------------

    def insert_processed_outcome(self, source: str, source_event_id: str, outcome: str) -> bool:
        """INSERT OR IGNORE; returns True iff this was a genuine new row."""
        pid = processed_outcome_id(source, source_event_id)
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO processed_outcomes
                       (id, source, source_event_id, outcome, processed_at)
                       VALUES (?,?,?,?,?)""",
                    (pid, source, source_event_id, outcome, now),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed == 1

    # --- notifications ---------------------------------------------------------

    def record_notification_attempt(self, pending_review_id_: str, delivered: bool) -> None:
        """Advance notification_state/attempts per the bounded 3-attempt schedule."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT notification_attempts, last_notified_at, next_retry_at "
                    "FROM pending_review WHERE id=?",
                    (pending_review_id_,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"pending_review {pending_review_id_!r} not found.")

                now = self._clock()
                attempts = row["notification_attempts"] + 1

                if delivered:
                    conn.execute(
                        """UPDATE pending_review
                           SET notification_state=?, notification_attempts=?,
                               last_notified_at=?, next_retry_at=NULL, updated_at=?
                           WHERE id=?""",
                        (NOTIF_NOTIFIED, attempts, _iso(now), _iso(now), pending_review_id_),
                    )
                    return

                if attempts >= NOTIFICATION_MAX_ATTEMPTS:
                    conn.execute(
                        """UPDATE pending_review
                           SET notification_state=?, notification_attempts=?,
                               next_retry_at=NULL, updated_at=?
                           WHERE id=?""",
                        (NOTIF_RETRIES_EXHAUSTED, attempts, _iso(now), pending_review_id_),
                    )
                    return

                if attempts == 1:
                    next_retry = now + _RETRY_1_DELAY
                else:
                    # 60 minutes from the *initial* attempt, not from this one.
                    initial = (
                        datetime.fromisoformat(row["last_notified_at"])
                        if row["last_notified_at"]
                        else now
                    )
                    next_retry = initial + _RETRY_2_DELAY_FROM_INITIAL

                conn.execute(
                    """UPDATE pending_review
                       SET notification_state=?, notification_attempts=?,
                           last_notified_at=COALESCE(last_notified_at, ?),
                           next_retry_at=?, updated_at=?
                       WHERE id=?""",
                    (
                        NOTIF_RETRY_SCHEDULED, attempts, _iso(now), _iso(next_retry),
                        _iso(now), pending_review_id_,
                    ),
                )

    def due_for_notification_retry(self) -> list[PendingReviewRow]:
        """Rows whose next_retry_at has passed. Only ever read inside a confirmed
        manual run; nothing in this module schedules or triggers this itself."""
        now_iso = self._now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM pending_review
                   WHERE notification_state=? AND next_retry_at IS NOT NULL
                     AND next_retry_at <= ?
                   ORDER BY created_at ASC, id ASC""",
                (NOTIF_RETRY_SCHEDULED, now_iso),
            ).fetchall()
        return [_row_to_pending_review(r) for r in rows]

    # --- ingestion_confirmations (manual-run gate) ------------------------------

    def issue_confirmation_token(self) -> tuple[str, str]:
        """Returns (token, expires_at_iso). Single-use, 300s TTL."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO ingestion_confirmations
                       (token_hash, issued_at, expires_at, state)
                       VALUES (?,?,?,'issued')""",
                    (_token_hash(token), _iso(now), _iso(expires)),
                )
        return token, _iso(expires)

    def consume_confirmation_token(self, token: str) -> bool:
        """Atomic single-use consumption; True iff exactly one row was updated."""
        now_iso = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE ingestion_confirmations SET state='used', used_at=?
                       WHERE token_hash=? AND state='issued' AND expires_at>?""",
                    (now_iso, _token_hash(token), now_iso),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed == 1

    def has_unaccepted_run(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM ingestion_runs WHERE status='awaiting_acceptance' "
                "ORDER BY started_at ASC LIMIT 1"
            ).fetchone()
        return row["run_id"] if row else None

    # --- ingestion_runs ----------------------------------------------------------

    def record_run(
        self,
        *,
        token: str,
        started_at: datetime,
        completed_at: datetime,
        admitted: int,
        override_admitted: int,
        discarded: int,
        pending_review_count: int,
        errors: int,
        critical_errors: int,
    ) -> str:
        run_id = hashlib.sha256(
            f"{_token_hash(token)}|{_iso(started_at)}".encode()
        ).hexdigest()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO ingestion_runs
                       (run_id, started_at, completed_at, status, admitted,
                        override_admitted, discarded, pending_review, errors,
                        critical_errors)
                       VALUES (?,?,?,'awaiting_acceptance',?,?,?,?,?,?)""",
                    (
                        run_id, _iso(started_at), _iso(completed_at), admitted,
                        override_admitted, discarded, pending_review_count, errors,
                        critical_errors,
                    ),
                )
        return run_id

    def accept_run(self, run_id: str) -> bool:
        now_iso = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE ingestion_runs SET status='accepted', accepted_at=?
                       WHERE run_id=? AND status='awaiting_acceptance'""",
                    (now_iso, run_id),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed == 1

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingestion_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    # --- action_approval_tokens ---------------------------------------------------

    def issue_action_approval_token(self, pending_review_id_: str, action: str) -> str:
        if action not in KNOWN_ACTIONS:
            raise ValueError(f"Unknown action {action!r}.")
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + timedelta(seconds=ACTION_APPROVAL_TTL_SECONDS)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO action_approval_tokens
                       (token_hash, pending_review_id, action, issued_at,
                        expires_at, state)
                       VALUES (?,?,?,?,?,'issued')""",
                    (_token_hash(token), pending_review_id_, action, _iso(now), _iso(expires)),
                )
        return token

    def consume_action_approval_token(
        self, token: str, pending_review_id_: str, action: str
    ) -> bool:
        now_iso = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """UPDATE action_approval_tokens SET state='used', used_at=?
                       WHERE token_hash=? AND pending_review_id=? AND action=?
                         AND state='issued' AND expires_at>?""",
                    (now_iso, _token_hash(token), pending_review_id_, action, now_iso),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed == 1

    # --- note_mirror (OPS-18 GHL note mirror crash-safe idempotency) -----------

    def get_note_mirror(self, event_id: str) -> NoteMirrorRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM note_mirror WHERE event_id=?", (event_id,)
            ).fetchone()
        return _row_to_note_mirror(row) if row else None

    def record_note_mirror_attempt(
        self, event_id: str, *, contact_id: str, content_hash: str
    ) -> None:
        """Record that a mirror attempt is starting for *event_id*.

        INSERT OR IGNORE creates the row on the first attempt; the follow-up
        UPDATE bumps ``attempts``/``last_attempt_at`` on every call, including
        retries after a crash. Never touches an already-``complete`` row, so
        a stray re-attempt after recovery cannot regress its state.
        """
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN")
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO note_mirror
                           (event_id, contact_id, note_id, status, content_hash,
                            attempts, created_at, updated_at, last_attempt_at)
                           VALUES (?,?,NULL,?,?,0,?,?,?)""",
                        (
                            event_id, contact_id, NOTE_MIRROR_STATUS_PENDING,
                            content_hash, now, now, now,
                        ),
                    )
                    conn.execute(
                        """UPDATE note_mirror
                           SET attempts=attempts+1, content_hash=?,
                               last_attempt_at=?, updated_at=?
                           WHERE event_id=? AND status != ?""",
                        (content_hash, now, now, event_id, NOTE_MIRROR_STATUS_COMPLETE),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

    def mark_note_mirror_complete(
        self, event_id: str, *, contact_id: str, note_id: str, content_hash: str
    ) -> None:
        """Idempotent: safe to call again for an event already marked complete
        (e.g. a retry that re-discovers the same note by marker)."""
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO note_mirror
                       (event_id, contact_id, note_id, status, content_hash,
                        attempts, created_at, updated_at, last_attempt_at)
                       VALUES (?,?,?,?,?,1,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET
                           contact_id=excluded.contact_id,
                           note_id=excluded.note_id,
                           status=excluded.status,
                           content_hash=excluded.content_hash,
                           updated_at=excluded.updated_at""",
                    (
                        event_id, contact_id, note_id, NOTE_MIRROR_STATUS_COMPLETE,
                        content_hash, now, now, now,
                    ),
                )

    # --- plaud_summary_state (OPS-110 match/transcript/summary tracking) -------

    def get_plaud_summary_state(self, plaud_recording_id: str) -> PlaudSummaryStateRow | None:
        rid = plaud_summary_state_id(plaud_recording_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plaud_summary_state WHERE id=?", (rid,)
            ).fetchone()
        return _row_to_plaud_summary_state(row) if row else None

    def upsert_plaud_summary_state(
        self,
        plaud_recording_id: str,
        *,
        match_status: str,
        desk_source_event_id: str | None = None,
        contact_id: str | None = None,
        transcript_status: str | None = None,
        summary_status: str | None = None,
        claude_output_hash: str | None = None,
        note_id: str | None = None,
        error_class: str | None = None,
    ) -> PlaudSummaryStateRow:
        """Forward-progressing per-Plaud-recording status row, keyed only by
        plaud_recording_id (idempotent on replay/overlap: the same
        recording always resolves to the same row).

        Every field left as None on an existing row preserves whatever was
        already recorded (or the fixed initial default, for a brand-new
        row); a value given overwrites. The one exception is
        *error_class*: it is always set exactly as given (including None,
        which clears it) -- a later successful stage is expected to pass
        error_class=None to clear a prior stage's failure.

        Never a raw handle, transcript, or summary body column -- only IDs,
        statuses, hashes, and this one content-free error classification.
        """
        rid = plaud_summary_state_id(plaud_recording_id)
        now = self._now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN")
                try:
                    row = conn.execute(
                        "SELECT id FROM plaud_summary_state WHERE id=?", (rid,)
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            """INSERT INTO plaud_summary_state
                               (id, plaud_recording_id, desk_source_event_id,
                                contact_id, match_status, transcript_status,
                                summary_status, claude_output_hash, note_id,
                                error_class, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                rid, plaud_recording_id, desk_source_event_id,
                                contact_id, match_status,
                                transcript_status or PLAUD_TRANSCRIPT_STATUS_NOT_FETCHED,
                                summary_status or PLAUD_SUMMARY_STATUS_NOT_STARTED,
                                claude_output_hash, note_id, error_class, now, now,
                            ),
                        )
                    else:
                        conn.execute(
                            """UPDATE plaud_summary_state
                               SET desk_source_event_id=COALESCE(?, desk_source_event_id),
                                   contact_id=COALESCE(?, contact_id),
                                   match_status=?,
                                   transcript_status=COALESCE(?, transcript_status),
                                   summary_status=COALESCE(?, summary_status),
                                   claude_output_hash=COALESCE(?, claude_output_hash),
                                   note_id=COALESCE(?, note_id),
                                   error_class=?,
                                   updated_at=?
                               WHERE id=?""",
                            (
                                desk_source_event_id, contact_id, match_status,
                                transcript_status, summary_status, claude_output_hash,
                                note_id, error_class, now, rid,
                            ),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return self.get_plaud_summary_state(plaud_recording_id)


# ---------------------------------------------------------------------------
# Pending Call Reviews (list_pending_call_reviews): internal function only.
# ---------------------------------------------------------------------------

#: Exactly these fields reach the agent-facing surface. Never source_event_id,
#: display_name, masked_labels, resolved_contact_id, idempotency_key,
#: failure_stage/detail, or raw handle.
_PENDING_REVIEW_SAFE_FIELDS = (
    "id", "source", "occurred_at", "duration_s", "direction", "answered",
    "match_outcome", "status", "notification_state",
)


def _row_age_seconds(row: PendingReviewRow, now: datetime) -> float:
    created = datetime.fromisoformat(row.created_at)
    return max(0.0, (now - created).total_seconds())


def list_pending_call_reviews(
    state_db: IngestionStateDb,
    *,
    status: str | None = None,
    source: str | None = None,
    match_outcome: str | None = None,
    limit: int = 50,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Read-only, bounded, sanitized Pending Call Reviews surface.

    Returns {"pending_review_count": int, "oldest_pending_at": str|None, "rows": [...]}.
    Every row is restricted to _PENDING_REVIEW_SAFE_FIELDS plus a computed
    `age_seconds`. Mutation is impossible here: this function issues no writes
    and exposes no action token.
    """
    now = (clock or _utc_now)()
    rows = state_db.query_pending_review(
        status=status, source=source, match_outcome=match_outcome,
        limit=limit, order="oldest_first",
    )
    safe_rows = []
    for r in rows:
        d = {k: getattr(r, k) for k in _PENDING_REVIEW_SAFE_FIELDS}
        d["age_seconds"] = _row_age_seconds(r, now)
        # The explicit `id` field is preserved unchanged; every other
        # projected field still goes through sanitize_output.
        row_id = d.pop("id")
        safe_rows.append({"id": row_id, **sanitize_output(d)})
    return {
        "pending_review_count": state_db.count_unresolved_pending_review(),
        "oldest_pending_at": state_db.oldest_pending_at(),
        "rows": safe_rows,
    }


# ---------------------------------------------------------------------------
# OPS-114 report projection (build_ops114_pending_review_summary): a
# separate, deliberately different allowlist from _PENDING_REVIEW_SAFE_FIELDS
# above -- that one backs the agent-facing list_pending_call_reviews tool
# and has its own pinned shape (status, notification_state; no display_name
# or masked_labels). This one feeds the OPS-114 scheduled Desk automation's
# stable, content-safe incident detail for shared cron Amber.
# ---------------------------------------------------------------------------

#: Exactly these fields (plus masked_labels, added explicitly below) reach
#: the OPS-114 report projection. Never source_event_id, resolved_contact_id,
#: idempotency_key, failure_stage/detail, notification_state, status, or any
#: raw handle -- this table has no raw handle/transcript/note-body column at
#: all (see the module docstring), so there is nothing further to redact
#: beyond the field allowlist itself.
_OPS114_REPORT_SAFE_FIELDS = (
    "id", "display_name", "occurred_at", "duration_s", "direction",
    "answered", "decision", "match_outcome",
)


def build_ops114_pending_review_summary(
    state_db: IngestionStateDb, *, limit: int = 50
) -> dict[str, Any]:
    """Bounded, sorted, content-safe projection of every currently
    unresolved pending_review row, for the OPS-114 scheduled Desk automation
    to hand to the shared cron Amber router as one actionable incident.

    At most *limit* (default and hard cap for this call site: 50) items,
    ordered by occurred_at then id -- the same deterministic order as
    `query_unresolved_pending_review`. Identical pending state always
    produces an identical return value (and, once serialized with
    sort_keys=True by the automation's own `_emit`, byte-stable JSON): the
    cron Amber fingerprint depends on that stability to avoid spurious
    re-alerts and to properly suppress/recover as state changes.
    """
    total = state_db.count_unresolved_pending_review()
    rows = state_db.query_unresolved_pending_review(limit=limit)
    items = []
    for r in rows:
        d = {k: getattr(r, k) for k in _OPS114_REPORT_SAFE_FIELDS}
        row_id = d.pop("id")
        # Only the free-text/label-shaped fields go through sanitize_output
        # as a defense-in-depth pass -- occurred_at is an ISO-8601
        # timestamp (digit-hyphen groups) that the generic phone-like
        # pattern would otherwise partially match and corrupt, and
        # duration_s/direction/answered/decision/match_outcome are fixed
        # structured/enum values that never carry raw contact data.
        d["display_name"] = sanitize_output(d["display_name"])
        d["masked_labels"] = sanitize_output(r.masked_labels)
        items.append({"pending_review_id": row_id, **d})
    return {
        "pending_review_count": total,
        "truncated": total > len(items),
        "items": items,
    }
