"""Durable terminal-state store for background process sessions.

Why this exists
---------------
``ProcessRegistry`` keeps every session in memory (``_running`` / ``_finished``)
and checkpoints only the *running* ones to ``~/.hermes/processes.json``
(``_write_checkpoint`` skips any session with ``exited=True``).  A session that
is killed during gateway shutdown — ``kill_all()`` from
``_kill_tool_subprocesses`` — is marked ``exited=True`` and moved to
``_finished`` in the same call, so it is excluded from the very next checkpoint
write.  When the gateway process then dies (systemd SIGTERM, OOM, crash), all
in-memory state goes with it and the next boot's ``recover_from_checkpoint()``
has no record the session ever existed: ``poll(session_id)`` answers
``{"status": "not_found"}`` instead of the real terminal record
(``exit_code=-15``, ``completion_reason="killed"``), silently erasing the
outcome of a real background job.

This module is the missing half: a tiny SQLite table holding TERMINAL records
only (exit code, completion reason, exit timestamp, command, session id), so a
restarted gateway can surface the real outcome.

Relationship to ``tools/async_delegation.py``
---------------------------------------------
This deliberately MIRRORS the durable-record pattern in ``async_delegation.py``
(WAL-with-fallback schema init, a ``_transaction()`` contextmanager that always
closes the connection so WAL/SHM fds cannot leak, ``owner_pid`` +
``owner_started_at`` PID-identity recovery guarding against PID reuse, bounded
retention pruning) — but shares NO runtime state with it: its own database
file, its own lock, its own retention policy.  ``async_delegation``'s
``_DB_LOCK`` serializes every delegation dispatch, completion, and
delivery-claim; routing process kills through it would let a burst of killed
sessions delay ``delegate_task`` completion delivery.  Nothing here touches
``completion_queue``: this store is about what ``poll()`` returns after a
restart, not about notification delivery.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

# Terminal records are small (one row, no output buffer) so retention can be
# generous — the whole point is answering a poll() that arrives long after the
# gateway that ran the process is gone.
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETAINED = 2000

# Pruning is amortized rather than run on every write: the write path is hot
# (see ProcessRegistry._move_to_finished) and a prune costs a COUNT plus two
# DELETEs.  One prune per 64 writes keeps the table bounded without paying for
# it on each kill.  Recovery always prunes.
_PRUNE_EVERY = 64

_DB_LOCK = threading.Lock()
_writes_since_prune = 0
# Schema init is idempotent but not free (a PRAGMA + DDL round trip). Remember
# which database file has already been initialized in THIS process; keyed by
# path because tests relocate HERMES_HOME per test.
_schema_ready_for: Optional[str] = None
# (pid, kernel start time) of THIS process — see _owner_identity().
_owner_identity_cache: Optional[tuple] = None


def _db_path():
    """Path to the terminal-state database.

    Resolved lazily (not at import time) so a relocated ``HERMES_HOME`` — which
    the test suite does per test — is honored.
    """
    return get_hermes_home() / "processes.db"


def _connect() -> sqlite3.Connection:
    global _schema_ready_for
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        if _schema_ready_for != str(path):
            _initialize_schema(conn)
            _schema_ready_for = str(path)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="processes.db (process_terminal_store)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS process_terminal_states (
            session_id TEXT PRIMARY KEY,
            command TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            session_key TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            pid_scope TEXT NOT NULL DEFAULT 'host',
            exit_code INTEGER,
            completion_reason TEXT NOT NULL DEFAULT 'exited',
            termination_source TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL DEFAULT 0,
            exited_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            extra_json TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_process_terminal_exited_at "
        "ON process_terminal_states(exited_at)"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection, so ``with _connect()`` alone
    leaks the connection and its WAL/SHM descriptors until GC.  Same hazard,
    same fix as ``async_delegation._transaction``.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_identity() -> tuple:
    """(pid, kernel start time) of the process recording this row.

    The start time is what makes recovery safe against PID reuse: on the next
    boot a bare ``pid_exists`` check can be satisfied by an unrelated process
    that inherited the number.

    Cached: our own identity cannot change for the life of the process, and
    resolving it costs a ``/proc`` read (Linux) or a psutil lookup (macOS,
    Windows) that has no business running on every kill.  Re-resolved after a
    fork, since the child gets a new PID.
    """
    global _owner_identity_cache
    pid = os.getpid()
    cached = _owner_identity_cache
    if cached is not None and cached[0] == pid:
        return cached
    try:
        from gateway.status import get_process_start_time
        started = get_process_start_time(pid)
    except Exception:
        started = None
    _owner_identity_cache = (pid, started)
    return _owner_identity_cache


def record_terminal_state(session: Any, *, command: Optional[str] = None) -> bool:
    """Persist ONE terminal record for ``session``. Returns True on success.

    Cost profile.  This runs from ``ProcessRegistry._move_to_finished``, which
    ``kill_all()`` reaches once per session it ACTUALLY kills — including
    ``run_agent.Agent.cleanup()``'s ``kill_all(task_id=...)``, a hot path that
    fires on every agent/subagent/cron teardown.  One call is: a single local
    SQLite connect, one-row ``INSERT OR REPLACE``, close.  Measured ~0.5 ms on
    a warm database (vs ~0.3 ms for the ``_write_checkpoint()`` JSON rewrite
    already on the same line).  No network I/O, no lock held across an await
    (every caller in the chain is synchronous), and nothing that scales with
    the number of running sessions.  Crucially, ``kill_all()`` with no live
    targets never reaches here at all, so the steady-state cleanup cost stays
    exactly zero — the database file is not even created.

    Best-effort: a failure here must never break process teardown, so all
    exceptions are swallowed (logged at debug, like ``_write_checkpoint``).
    """
    global _writes_since_prune
    if session is None or not getattr(session, "id", ""):
        return False
    if not getattr(session, "exited", False):
        # Only TERMINAL state belongs in this store.
        return False

    try:
        from agent.redact import redact_sensitive_text
        raw_command = command if command is not None else getattr(session, "command", "")
        # The checkpoint file redacts inline credentials before hitting disk
        # (#77484); this store holds the same command string and must too.
        safe_command = redact_sensitive_text(raw_command or "", code_file=True)
    except Exception:
        safe_command = ""

    owner_pid, owner_started_at = _owner_identity()
    now = time.time()
    exit_code = getattr(session, "exit_code", None)
    row = (
        session.id,
        safe_command,
        getattr(session, "task_id", "") or "",
        getattr(session, "session_key", "") or "",
        getattr(session, "pid", None),
        getattr(session, "pid_scope", "host") or "host",
        int(exit_code) if isinstance(exit_code, int) else None,
        getattr(session, "completion_reason", "exited") or "exited",
        getattr(session, "termination_source", "") or "",
        float(getattr(session, "started_at", 0.0) or 0.0),
        now,
        owner_pid,
        owner_started_at,
        json.dumps({"detached": bool(getattr(session, "detached", False))}),
    )

    try:
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO process_terminal_states
                   (session_id, command, task_id, session_key, pid, pid_scope,
                    exit_code, completion_reason, termination_source,
                    started_at, exited_at, owner_pid, owner_started_at, extra_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )
            _writes_since_prune += 1
            if _writes_since_prune >= _PRUNE_EVERY:
                _writes_since_prune = 0
                _prune_locked(conn, now)
        return True
    except Exception as e:
        logger.debug("Failed to persist terminal state for %s: %s", session.id, e, exc_info=True)
        return False


def _prune_locked(conn: sqlite3.Connection, now: float) -> None:
    """Bound the table. Caller holds ``_DB_LOCK`` and an open transaction."""
    conn.execute(
        "DELETE FROM process_terminal_states WHERE exited_at < ?",
        (now - RETENTION_SECONDS,),
    )
    total = conn.execute("SELECT COUNT(*) FROM process_terminal_states").fetchone()[0]
    excess = max(0, total - MAX_RETAINED)
    if excess:
        conn.execute(
            """DELETE FROM process_terminal_states WHERE session_id IN (
                 SELECT session_id FROM process_terminal_states
                 ORDER BY exited_at ASC LIMIT ?
               )""",
            (excess,),
        )


def _row_to_dict(row) -> Dict[str, Any]:
    (session_id, command, task_id, session_key, pid, pid_scope, exit_code,
     completion_reason, termination_source, started_at, exited_at,
     owner_pid, owner_started_at, extra_json) = row
    try:
        extra = json.loads(extra_json or "{}")
    except Exception:
        extra = {}
    return {
        "session_id": session_id,
        "command": command or "",
        "task_id": task_id or "",
        "session_key": session_key or "",
        "pid": pid,
        "pid_scope": pid_scope or "host",
        "exit_code": exit_code,
        "completion_reason": completion_reason or "exited",
        "termination_source": termination_source or "",
        "started_at": started_at or 0.0,
        "exited_at": exited_at or 0.0,
        "owner_pid": owner_pid,
        "owner_started_at": owner_started_at,
        "detached": bool(extra.get("detached")),
    }


_SELECT_COLUMNS = (
    "session_id, command, task_id, session_key, pid, pid_scope, exit_code, "
    "completion_reason, termination_source, started_at, exited_at, "
    "owner_pid, owner_started_at, extra_json"
)


def get_terminal_state(session_id: str) -> Optional[Dict[str, Any]]:
    """Look up one terminal record by session id (primary-key hit), or None."""
    if not session_id:
        return None
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM process_terminal_states WHERE session_id=?",
                (session_id,),
            ).fetchone()
    except Exception as e:
        logger.debug("Terminal-state lookup failed for %s: %s", session_id, e, exc_info=True)
        return None
    return _row_to_dict(row) if row else None


def _owner_is_live(pid, started) -> bool:
    """True only if the recording process is still alive AND still itself.

    Same PID-reuse guard ``async_delegation.recover_abandoned_delegations``
    applies: liveness alone is satisfiable by an unrelated process that
    inherited the number after a reboot.
    """
    if not pid:
        return False
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return False
    try:
        if not _pid_exists(int(pid)):
            return False
        if started is None:
            return True
        return get_process_start_time(int(pid)) == int(started)
    except Exception:
        return False


def recover_terminal_states(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Terminal records written by a process that is no longer running.

    These are exactly the records a restarted gateway must resurrect: their
    owner recorded a real outcome and then died before (or without) anything
    else persisting it.  Records still owned by a live process — this process,
    or a concurrently running sibling — are left alone; their owner's in-memory
    registry is authoritative for them.

    ``limit`` caps how many of the MOST RECENT records are considered, so a
    caller that rehydrates into memory cannot be handed the whole retention
    window at boot.  Older records stay queryable by id through
    :func:`get_terminal_state`.  Returned oldest-first.

    Also prunes, so an idle install doesn't accumulate rows forever when the
    amortized write-path prune never trips.
    """
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            _prune_locked(conn, now)
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM process_terminal_states "
                "ORDER BY exited_at DESC"
            )
            if limit is not None:
                rows = conn.execute(sql + " LIMIT ?", (int(limit),)).fetchall()
            else:
                rows = conn.execute(sql).fetchall()
    except Exception as e:
        logger.debug("Terminal-state recovery query failed: %s", e, exc_info=True)
        return []

    recovered = []
    for row in rows:
        record = _row_to_dict(row)
        if _owner_is_live(record.get("owner_pid"), record.get("owner_started_at")):
            continue
        recovered.append(record)
    recovered.reverse()  # oldest-first
    return recovered


def _reset_for_tests() -> None:
    """Drop cached per-process state so a relocated HERMES_HOME is picked up."""
    global _schema_ready_for, _writes_since_prune, _owner_identity_cache
    with _DB_LOCK:
        _schema_ready_for = None
        _writes_since_prune = 0
        _owner_identity_cache = None
