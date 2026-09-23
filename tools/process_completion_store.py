"""Durable completion-notification store for background process sessions.

Why this exists
---------------
``ProcessRegistry._move_to_finished()`` enqueues a ``type="completion"`` event
onto the in-memory ``completion_queue`` whenever a session with
``notify_on_complete`` exits.  Consumers turn that event into the synthetic
``[IMPORTANT: ...]`` message a user actually sees (the gateway's per-process
watcher, the CLI/TUI post-turn drain).

``completion_queue`` is a plain ``queue.Queue``: if the gateway process dies
between the enqueue and the delivery — the shutdown sequence disconnects the
platform adapter roughly 10 ms before the kill, so this window is real and is
hit routinely — the event is garbage-collected with the process.  Nothing on
disk records that a notification was owed, so the next boot never sends it and
the user simply never hears that their job finished.  This is distinct from the
terminal-state gap ``process_terminal_store`` closes: that one makes ``poll()``
correct after a restart, this one makes the NOTIFICATION arrive.

Why a sibling module instead of a new table inside ``process_terminal_store``
----------------------------------------------------------------------------
Same database FILE (``processes.db``) — no new artifact to manage — but its own
table, its own ``_DB_LOCK`` and its own module-level state, for two reasons:

1. ``process_terminal_store`` is shipped, load-bearing code for ``poll()``
   correctness.  Delivery tracking has a different write cadence (only
   ``notify_on_complete`` sessions), a different lifecycle (rows mutate from
   ``pending`` to ``delivered``) and a different failure mode.  Bolting it onto
   the terminal store would have delivery writes bumping the terminal store's
   amortized prune counter and sharing its lock for no reason.
2. Deliberately NOT ``async_delegation``'s ``async_delegations`` table or its
   ``_DB_LOCK``: that lock serializes every delegation dispatch, completion and
   delivery claim, so routing process completions through it would let a burst
   of exiting background processes delay ``delegate_task`` delivery.  Same
   isolation reasoning ``process_terminal_store`` documents.

The delivery-state machine mirrors ``async_delegation``'s proven one:
``pending`` → ``delivered`` (acknowledged, never replayed again) or
``dropped`` (replayed too many times without ever being acknowledged).
Exactly-once across a restart comes from the same two-part guard: the replay
query filters on ``delivery_state='pending'``, and bounded retention keeps the
table from growing without limit.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_cli.config import get_hermes_home
# Retention policy is deliberately IDENTICAL to the terminal store's — these
# rows are the same shape of small, one-per-terminated-session record, and a
# completion notification that is a week stale is no more deliverable than a
# terminal record that old.  Imported rather than re-declared so the two can
# never silently drift apart.
from tools.process_terminal_store import (
    MAX_RETAINED,
    RETENTION_SECONDS,
    _owner_identity,
    _owner_is_live,
)

logger = logging.getLogger(__name__)

# A pending event that has been replayed this many times without any consumer
# ever acknowledging delivery is undeliverable in practice (no route survives,
# the originating chat is gone, the formatter rejects it).  Dropping it stops
# an unroutable completion from being re-injected on every boot forever — the
# same bound ``async_delegation`` puts on delivery attempts.
MAX_REPLAY_ATTEMPTS = 5

# Staleness cap for restart replay, mirroring
# ``async_delegation._MAX_COMPLETION_REPLAY_AGE_S``: a pending completion
# older than this is terminally dropped instead of replayed as a fresh
# synthetic turn. Replaying a week-old completion re-wakes a session nobody
# is waiting on and, worse, does it once per finished process (see
# MAX_DELIVERY_ATTEMPTS below for the sibling bound on claim/release churn).
# 48h keeps overnight/weekend results deliverable.
MAX_REPLAY_AGE_SECONDS = 48 * 3600.0

# Bounds the atomic claim/release cycle (below), separately from
# MAX_REPLAY_ATTEMPTS which bounds replay ACROSS BOOTS. A row whose delivery
# keeps being claimed and released — by one or several competing consumers,
# within or across boots — this many times converges to a terminal
# ``dropped`` state instead of retrying forever.
MAX_DELIVERY_ATTEMPTS = 8

# A claim older than this is presumed abandoned by a dead holder (the
# process that claimed it crashed before ack/release) and may be re-claimed
# by a fresh consumer. Mirrors async_delegation's claim-staleness window.
_CLAIM_STALE_SECONDS = 300

# Same amortization as the terminal store: the write path runs from
# ``_move_to_finished`` and a prune costs a COUNT plus two DELETEs.
_PRUNE_EVERY = 64

_DB_LOCK = threading.Lock()
_writes_since_prune = 0
_schema_ready_for: Optional[str] = None
# Session ids already replayed onto a queue by THIS process.  Startup recovery
# must be idempotent: a retried/duplicated startup call must not enqueue the
# same completion twice.  The durable row deliberately stays ``pending`` until
# a consumer acknowledges real delivery, so this guard — not the row state —
# is what makes a double call a no-op.
_restored_this_process: set = set()


def _db_path():
    """Path to the process database (shared file with the terminal store).

    Resolved lazily so a relocated ``HERMES_HOME`` (the test suite does this
    per test) is honored.
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
        # Never leak a just-opened connection when PRAGMA/DDL fails.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="processes.db (process_completion_store)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS process_completion_events (
            session_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL DEFAULT '',
            event_json TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            replay_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_process_completion_created_at "
        "ON process_completion_events(created_at)"
    )
    # Additive migration (OPS-117): atomic claim-lifecycle columns for an
    # exactly-once delivery gate across concurrent/crash-restarted consumers
    # (claim_completion_delivery / release / drop / complete below).
    # Idempotent on an existing processes.db — ADD COLUMN only when absent,
    # so a table created before this migration (or the 20 pre-existing
    # pending rows carried into this build) reads and writes unchanged.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(process_completion_events)")
    }
    for name, sql_type in (
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE process_completion_events ADD COLUMN {name} {sql_type}"
            )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back; they
    do not close, so ``with _connect()`` alone leaks the connection and its
    WAL/SHM descriptors until GC.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _redacted_event(evt: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of ``evt`` with the command and output tail redacted for disk.

    The JSON checkpoint and the terminal store both redact inline credentials
    before writing (#77484); this row holds the same command string plus a
    slice of raw process output, so it must too.  Best-effort: a redaction
    failure must never cost us the durability record, so the original values
    are kept if redaction blows up.
    """
    safe = dict(evt)
    command = str(evt.get("command") or "")
    try:
        from agent.redact import redact_sensitive_text
        safe["command"] = redact_sensitive_text(command, code_file=True)
    except Exception:
        logger.debug("Command redaction failed for durable completion", exc_info=True)
    output = evt.get("output")
    if output:
        try:
            from agent.redact import redact_terminal_output
            safe["output"] = redact_terminal_output(str(output), command)
        except Exception:
            logger.debug("Output redaction failed for durable completion", exc_info=True)
    return safe


def record_pending_completion(evt: Dict[str, Any]) -> bool:
    """Persist ONE completion event as ``pending``. Returns True on success.

    Called from ``ProcessRegistry._move_to_finished`` immediately BEFORE the
    matching ``completion_queue.put()``, so a process death between the two
    leaves the durable row behind (a replayed-but-already-delivered event is
    prevented by the delivery ack, whereas a lost row can never be recovered).

    ``INSERT OR IGNORE`` rather than ``OR REPLACE``: session ids are unique per
    incarnation, and ignoring means a row that already reached ``delivered``
    can never be resurrected into ``pending`` by a late duplicate producer —
    the "not twice" half of exactly-once.

    Best-effort: process teardown must never break because SQLite is unhappy,
    so every exception is swallowed (logged at debug, like ``_write_checkpoint``).
    """
    global _writes_since_prune
    session_id = str(evt.get("session_id") or "")
    if not session_id:
        return False

    owner_pid, owner_started_at = _owner_identity()
    now = time.time()
    try:
        payload = json.dumps(_redacted_event(evt))
    except Exception as e:
        logger.debug("Could not serialize completion event %s: %s", session_id, e)
        return False

    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO process_completion_events
                   (session_id, session_key, event_json, delivery_state,
                    replay_count, created_at, delivered_at, owner_pid,
                    owner_started_at)
                   VALUES (?, ?, ?, 'pending', 0, ?, NULL, ?, ?)""",
                (
                    session_id,
                    str(evt.get("session_key") or ""),
                    payload,
                    now,
                    owner_pid,
                    owner_started_at,
                ),
            )
            inserted = cur.rowcount == 1
            _writes_since_prune += 1
            if _writes_since_prune >= _PRUNE_EVERY:
                _writes_since_prune = 0
                _prune_locked(conn, now)
        return inserted
    except Exception as e:
        logger.debug(
            "Failed to persist pending completion for %s: %s", session_id, e, exc_info=True
        )
        return False


def mark_completion_delivered(session_id: str) -> bool:
    """Acknowledge that a completion notification actually reached a consumer.

    Returns True when this call is the one that flipped the row.  After this,
    the event is never replayed again — the "not twice" guard.  Idempotent: a
    second call returns False and changes nothing.
    """
    if not session_id:
        return False
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_state='delivered', delivered_at=?
                   WHERE session_id=? AND delivery_state='pending'""",
                (now, session_id),
            )
            return cur.rowcount == 1
    except Exception as e:
        logger.debug(
            "Could not mark completion %s delivered: %s", session_id, e, exc_info=True
        )
        return False


# ---------------------------------------------------------------------------
# Atomic claim lifecycle (OPS-117)
# ---------------------------------------------------------------------------
# ``mark_completion_delivered`` above is a direct, unconditional ack: correct
# for a single in-process owner (the live per-process watcher, the CLI's
# drain) where nothing else could be racing to deliver the same row. It is
# NOT enough once more than one consumer can compete for the SAME pending row
# — two gateway delivery paths on one boot, a startup replay racing a
# still-live watcher, or a TUI poller thread racing the gateway — because
# nothing stops both from independently deciding they delivered it.
#
# This trio (mirrors ``tools.async_delegation``'s proven claim/release/
# drop/complete state machine exactly, down to the staleness window and
# attempt cap) adds a real compare-and-swap: ``claim`` only succeeds for ONE
# caller at a time, and only that caller's ``claim_id`` can later transition
# the row to ``delivered``/``dropped``/back-to-``pending``. A crashed holder's
# claim goes stale after ``_CLAIM_STALE_SECONDS`` and becomes claimable again
# — crash-safe and restart-safe by construction, not by a best-effort guess.


def claim_completion_delivery(session_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes.

    A missing row (no durable record for this session id — a legacy
    queue-only event, or a caller that never persisted one) is treated as an
    uncontested claim: there is nothing durable to race against, so the
    caller proceeds exactly as it always could. This mirrors
    ``async_delegation.claim_completion_delivery``'s "legacy event" case.
    """
    if not session_id or not claim_id:
        return False
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                "SELECT delivery_state FROM process_completion_events WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return True
            cur = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_claim=?, delivery_claimed_at=?,
                       delivery_attempts=delivery_attempts+1
                   WHERE session_id=? AND delivery_state='pending'
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
                (claim_id, now, session_id, now - _CLAIM_STALE_SECONDS),
            )
            return cur.rowcount == 1
    except Exception as e:
        logger.debug(
            "Could not claim completion delivery for %s: %s", session_id, e, exc_info=True
        )
        return False


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable process-completion event's delivery.

    Returns ``""`` (falsy, not ``None``) for a non-``completion`` event or one
    without a session id — "no claim needed, proceed" — so a caller checking
    only ``is None`` treats it as uncontested, the same convention
    ``async_delegation.claim_event_delivery`` uses. Returns ``None`` when
    another consumer currently holds the claim.
    """
    if evt.get("type") != "completion":
        return ""
    session_id = str(evt.get("session_id") or "")
    if not session_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(session_id, claim_id) else None


def release_completion_delivery(session_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once ``MAX_DELIVERY_ATTEMPTS``
    is exhausted the row converges to a terminal ``dropped`` state instead of
    returning to ``pending`` — otherwise an undeliverable completion would
    keep being reclaimed forever within one boot, and (via
    ``restore_pending_completions``) replay again on every restart.
    """
    if not session_id or not claim_id:
        return False
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            capped = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_state='dropped', delivery_claim=NULL,
                       delivery_claimed_at=NULL
                   WHERE session_id=? AND delivery_state='pending'
                     AND delivery_claim=? AND delivery_attempts>=?""",
                (session_id, claim_id, MAX_DELIVERY_ATTEMPTS),
            )
            if capped.rowcount == 1:
                logger.warning(
                    "Process completion %s exhausted its %d delivery "
                    "attempts; marking terminally dropped (output remains "
                    "available via process(action='log')).",
                    session_id, MAX_DELIVERY_ATTEMPTS,
                )
                return True
            cur = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_claim=NULL, delivery_claimed_at=NULL
                   WHERE session_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (session_id, claim_id),
            )
            return cur.rowcount == 1
    except Exception as e:
        logger.debug(
            "Could not release completion delivery claim for %s: %s",
            session_id, e, exc_info=True,
        )
        return False


def drop_completion_delivery(session_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone.

    Used when the session-boundary pre-flight proves delivery can never
    succeed — a user boundary such as ``/new``, or an unresolvable/archived
    session. Marking the row ``dropped`` (not left ``pending``) stops it from
    replaying on the next boot only to be dropped again every time. No raw
    command/output is logged at the call sites that use this — session ids
    only.
    """
    if not session_id or not claim_id:
        return False
    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_state='dropped', delivery_claim=NULL,
                       delivery_claimed_at=NULL
                   WHERE session_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (session_id, claim_id),
            )
            return cur.rowcount == 1
    except Exception as e:
        logger.debug(
            "Could not drop completion delivery claim for %s: %s",
            session_id, e, exc_info=True,
        )
        return False


def complete_completion_delivery(session_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    if not session_id or not claim_id:
        return False
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            cur = conn.execute(
                """UPDATE process_completion_events
                   SET delivery_state='delivered', delivered_at=?,
                       delivery_claim=NULL, delivery_claimed_at=NULL
                   WHERE session_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, session_id, claim_id),
            )
            return cur.rowcount == 1
    except Exception as e:
        logger.debug(
            "Could not complete completion delivery for %s: %s",
            session_id, e, exc_info=True,
        )
        return False


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "completion":
        complete_completion_delivery(str(evt.get("session_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "completion":
        release_completion_delivery(str(evt.get("session_id") or ""), claim_id)


def drop_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "completion":
        drop_completion_delivery(str(evt.get("session_id") or ""), claim_id)


def restore_pending_completions(target_queue) -> int:
    """Re-enqueue completions a dead process never delivered. Returns the count.

    Mirrors ``async_delegation.restore_undelivered_completions``: each restored
    event is stamped ``restored=True`` IN MEMORY ONLY (never persisted), so a
    consumer that cares can tell a replay from a fresh completion while
    everything that doesn't care treats it identically.

    Two guards keep this exactly-once:

    - Rows whose owner process is still alive are skipped.  Their event is
      still sitting in that process's in-memory queue; replaying it here would
      be the duplicate ``[IMPORTANT: ...]`` message this whole mechanism exists
      to avoid.  Same PID-identity check (pid + kernel start time, so a reused
      PID cannot masquerade as the owner) the terminal store uses.
    - ``_restored_this_process`` makes a repeated call within one process a
      no-op, so a flaky/retried startup path cannot enqueue the same event
      twice.  Rows stay ``pending`` until real delivery is acknowledged, so
      this in-memory guard — not the row state — is what provides idempotency.

    A row replayed ``MAX_REPLAY_ATTEMPTS`` times without ever being
    acknowledged is marked ``dropped`` instead of replayed again.
    """
    now = time.time()
    try:
        with _DB_LOCK, _transaction() as conn:
            _prune_locked(conn, now)
            rows = conn.execute(
                """SELECT session_id, event_json, replay_count, owner_pid,
                          owner_started_at, created_at
                   FROM process_completion_events
                   WHERE delivery_state='pending'
                   ORDER BY created_at, session_id"""
            ).fetchall()

            restored = []
            for (
                session_id, payload, replay_count, owner_pid, owner_started_at,
                created_at,
            ) in rows:
                if session_id in _restored_this_process:
                    continue
                if _owner_is_live(owner_pid, owner_started_at):
                    continue
                if created_at and (now - created_at) > MAX_REPLAY_AGE_SECONDS:
                    conn.execute(
                        "UPDATE process_completion_events SET delivery_state='dropped' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
                    logger.warning(
                        "Dropping background completion %s: pending completion "
                        "is %.1fh old (cap %.1fh); the result stays queryable "
                        "via process(action='log') but is not replayed",
                        session_id, (now - created_at) / 3600.0,
                        MAX_REPLAY_AGE_SECONDS / 3600.0,
                    )
                    continue
                if (replay_count or 0) >= MAX_REPLAY_ATTEMPTS:
                    conn.execute(
                        "UPDATE process_completion_events SET delivery_state='dropped' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
                    logger.warning(
                        "Dropping background completion %s: replayed %s times "
                        "without a delivery acknowledgement",
                        session_id, replay_count,
                    )
                    continue
                try:
                    evt = json.loads(payload)
                except Exception:
                    logger.debug("Unreadable durable completion %s — dropping", session_id)
                    conn.execute(
                        "UPDATE process_completion_events SET delivery_state='dropped' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
                    continue
                if not isinstance(evt, dict):
                    continue
                evt["restored"] = True
                conn.execute(
                    "UPDATE process_completion_events SET replay_count=replay_count+1 "
                    "WHERE session_id=?",
                    (session_id,),
                )
                restored.append((session_id, evt))
            # The queue put happens INSIDE the lock, after the replay_count
            # bump is staged, so a concurrent restore cannot observe the same
            # row as un-replayed and enqueue it a second time.
            for session_id, evt in restored:
                _restored_this_process.add(session_id)
                target_queue.put(evt)
    except Exception as e:
        logger.debug("Pending-completion restore failed: %s", e, exc_info=True)
        return 0
    return len(restored)


def _prune_locked(conn: sqlite3.Connection, now: float) -> None:
    """Bound the table. Caller holds ``_DB_LOCK`` and an open transaction.

    Age-based first, then a hard row cap, using the same window and cap as the
    terminal store.  Pending rows are pruned by age too: a completion older
    than the retention window has no user left who wants to hear about it, and
    keeping it would mean an unbounded pending set on an install that never
    successfully delivers.
    """
    conn.execute(
        "DELETE FROM process_completion_events WHERE created_at < ?",
        (now - RETENTION_SECONDS,),
    )
    total = conn.execute("SELECT COUNT(*) FROM process_completion_events").fetchone()[0]
    excess = max(0, total - MAX_RETAINED)
    if excess:
        conn.execute(
            """DELETE FROM process_completion_events WHERE session_id IN (
                 SELECT session_id FROM process_completion_events
                 ORDER BY created_at ASC LIMIT ?
               )""",
            (excess,),
        )


def get_completion_record(session_id: str) -> Optional[Dict[str, Any]]:
    """One durable completion row by session id (primary-key hit), or None."""
    if not session_id:
        return None
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                """SELECT session_id, session_key, event_json, delivery_state,
                          replay_count, created_at, delivered_at, owner_pid,
                          owner_started_at, delivery_claim, delivery_claimed_at,
                          delivery_attempts
                   FROM process_completion_events WHERE session_id=?""",
                (session_id,),
            ).fetchone()
    except Exception as e:
        logger.debug("Completion lookup failed for %s: %s", session_id, e, exc_info=True)
        return None
    if row is None:
        return None
    try:
        event = json.loads(row[2])
    except Exception:
        event = {}
    return {
        "session_id": row[0],
        "session_key": row[1] or "",
        "event": event,
        "delivery_state": row[3],
        "replay_count": row[4] or 0,
        "created_at": row[5] or 0.0,
        "delivered_at": row[6],
        "owner_pid": row[7],
        "owner_started_at": row[8],
        "delivery_claim": row[9],
        "delivery_claimed_at": row[10],
        "delivery_attempts": row[11] or 0,
    }


def list_pending_completions() -> List[Dict[str, Any]]:
    """Every row still awaiting delivery acknowledgement (diagnostics/tests)."""
    try:
        with _DB_LOCK, _transaction() as conn:
            rows = conn.execute(
                "SELECT session_id FROM process_completion_events "
                "WHERE delivery_state='pending' ORDER BY created_at, session_id"
            ).fetchall()
    except Exception as e:
        logger.debug("Pending-completion listing failed: %s", e, exc_info=True)
        return []
    return [r for r in (get_completion_record(sid) for (sid,) in rows) if r]


def restored_session_ids() -> set:
    """Session ids THIS process replayed onto a queue at startup.

    The gateway's restored-completion watcher uses this to know whether it has
    any work at all (and which events on the shared queue are its own), so it
    can exit immediately — and stay exited — when nothing was replayed.
    """
    with _DB_LOCK:
        return set(_restored_this_process)


def _reset_for_tests() -> None:
    """Drop cached per-process state so a relocated HERMES_HOME is picked up."""
    global _schema_ready_for, _writes_since_prune
    with _DB_LOCK:
        _schema_ready_for = None
        _writes_since_prune = 0
        _restored_this_process.clear()
