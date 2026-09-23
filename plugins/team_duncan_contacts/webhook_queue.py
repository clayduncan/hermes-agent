"""OPS-114 durable queue for the Plaud webhook receiver's primary trigger.

A dedicated stdlib SQLite WAL database, under
``<hermes_home>/plugin-data/team_duncan_contacts/plaud_webhook_queue.db``.
Holds nothing but the immutable Plaud recording ID, an enqueue timestamp,
and (once drained) a drained timestamp -- never a title, transcript,
speaker name, summary, or secret.

This is the fix for the crash window the in-memory-only receiver had: a
process that returned HTTP 202 and then died before its background
dispatch thread finished had already told the sender "accepted", with
nothing durable to prove it. Every accepted event is committed here,
atomically, before the receiver is allowed to return 202 at all -- so
whatever drains this queue (the receiver's own best-effort dispatch
thread, or ``plaud_webhook_receiver.drain_pending_events`` run at process
startup) can always find and replay it after a crash, and a row is only
ever removed once the same automation runner it always used actually
completes it.

Dedupe is by the immutable recording ID: ``INSERT OR IGNORE`` on the
primary key means a replayed webhook event for an already-queued
recording never becomes a second row.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_DB_FILE_NAME = "plaud_webhook_queue.db"

_INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS webhook_events (
    plaud_recording_id  TEXT NOT NULL PRIMARY KEY,
    enqueued_at          REAL NOT NULL
);
"""


class WebhookQueue:
    """Durable pending-event queue. Every method opens and commits its own
    connection: this is a low-volume, per-event queue (one row per Plaud
    recording), not a hot path needing a pooled/long-lived connection."""

    def __init__(self, data_dir: Path) -> None:
        self._db_path = Path(data_dir) / _DB_FILE_NAME
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_INIT_SQL)

    def enqueue(self, plaud_recording_id: str, *, now: float) -> bool:
        """Durably record *plaud_recording_id* as pending, deduped by
        recording ID. Returns True once the write is committed (whether
        this call inserted a new row or the recording was already
        pending); returns False only if the write itself failed, which the
        caller must treat as a hard enqueue failure -- 503, never 202."""
        try:
            with self._lock:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO webhook_events "
                        "(plaud_recording_id, enqueued_at) VALUES (?, ?)",
                        (plaud_recording_id, now),
                    )
            return True
        except sqlite3.Error:
            return False

    def list_pending(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT plaud_recording_id FROM webhook_events ORDER BY enqueued_at ASC"
            ).fetchall()
        return [row[0] for row in rows]

    def remove(self, plaud_recording_id: str) -> None:
        """Drop *plaud_recording_id* from the pending set -- called only
        once the automation runner has actually completed it."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM webhook_events WHERE plaud_recording_id = ?",
                    (plaud_recording_id,),
                )


__all__ = ["WebhookQueue"]
