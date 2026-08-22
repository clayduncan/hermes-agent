"""Append-only audit log for writes that mutate records in external systems.

The invariant this module exists to enforce
===========================================

**A write must never reach its destination unless its audit line has already
landed on disk, successfully, first.**  The log append is a *precondition*, not a
companion action.  On every path — happy, error, and retry — the sequence is:

    1. gather ``before`` state (a read, never a write)
    2. append the audit line and ``os.fsync()`` it
    3. only if step 2 returned cleanly, call the destination API

If step 2 fails for any reason (disk full, permission denied, missing directory,
non-serializable payload, anything at all) it raises :class:`WriteAuditLogError`
and step 3 is never reached.  There is no rollback path, because a create cannot
be un-created without another API call that could also fail.  The log lands, or
the destination API is never called.

Why each write produces two lines
---------------------------------

The log format requires ``after`` — the object's *post-write* state.  That value
cannot exist before the write, and the write may not happen before the log line.
The only way to satisfy both is to append twice for one logical write:

* ``audit_phase: "intent"`` — appended **before** the destination call.  Carries
  ``before``, the operation, the actor and the trigger.  ``after`` is ``null``
  because it is not knowable yet.  This is the line that gates the write.
* ``audit_phase: "outcome"`` — appended **after** the destination call returned
  successfully.  Same fields, now with ``record_id`` resolved and ``after``
  populated (or flagged via ``after_fetch_failed``).

Both lines carry the full required field set and share an ``audit_id`` so a
query can pair them.  An intent line with no matching outcome line is itself
meaningful: it means a write was authorized and then did not complete.

How long entries are kept
-------------------------

Forever.  Nothing in this feature ever reduces the log: the only file mode used
is append, and no function anywhere in it takes files away or shortens them.  The
absence is structural — there is no such function to wire up by mistake later —
and ``tests/test_write_audit_log.py`` asserts it by scanning the source.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from hermes_constants import get_hermes_home

# ── Destination / operation vocabularies ─────────────────────────────────────

MSGRAPH_CONTACTS = "msgraph_contacts"
MSGRAPH_TASKS = "msgraph_tasks"
GHL_CONTACTS = "ghl_contacts"
CALENDLY_BOOKINGS = "calendly_bookings"
MSGRAPH_CALENDAR_EVENTS = "msgraph_calendar_events"

#: Destinations this log understands.  Extend when a new record type is wired up.
KNOWN_DESTINATIONS: tuple[str, ...] = (
    MSGRAPH_CONTACTS,
    MSGRAPH_TASKS,
    GHL_CONTACTS,
    CALENDLY_BOOKINGS,
    MSGRAPH_CALENDAR_EVENTS,
)

KNOWN_OPERATIONS: tuple[str, ...] = ("create", "update", "delete")

#: Exact literal stored in ``after`` when the destination write succeeded but the
#: post-write read-back did not.  Paired with ``after_fetch_failed: true`` so the
#: entry is findable by field query, not by string-spotting.
AFTER_UNKNOWN = "UNKNOWN — verify manually"

PHASE_INTENT = "intent"
PHASE_OUTCOME = "outcome"


# ── Errors ────────────────────────────────────────────────────────────────────


class WriteAuditLogError(RuntimeError):
    """The audit append failed, so the destination write was blocked.

    Raised *instead of* performing the destination call.  Callers must treat this
    exactly like a destination API failure: the write did not happen, and nothing
    about it may be reported as success.
    """

    #: Safe to retry — the destination never saw this write.
    write_completed = False


class WriteAuditOutcomeLogError(RuntimeError):
    """The destination write succeeded but its outcome line could not be appended.

    Distinct from :class:`WriteAuditLogError` because the remediation is the
    opposite: the write DID reach the destination and must NOT be retried.  The
    intent line for this operation is already on disk, so the write is recorded;
    what is missing is the post-write state.
    """

    #: Checked by callers with a blanket retry handler: retrying double-writes.
    write_completed = True


class MissingWriteTriggerError(ValueError):
    """A write was requested with no human-readable trigger string.

    Raised before the log append and before the destination call, so neither
    happens.
    """


def require_trigger(trigger: Any) -> str:
    """Return the validated trigger, or raise :class:`MissingWriteTriggerError`.

    Call this first in every write entrypoint — ahead of the ``before`` fetch, the
    audit append and the destination call — so a caller that forgot to say why it
    is writing gets rejected before anything observable happens.
    """
    if not isinstance(trigger, str) or not trigger.strip():
        raise MissingWriteTriggerError(
            "A write-audit trigger is required: pass trigger='<human-readable reason "
            "for this write>' (e.g. 'Nathan intro triage 2026-08-21'). "
            f"Got {trigger!r}. No audit line was written and no destination API was called."
        )
    return trigger.strip()


# ── Paths and timestamps ─────────────────────────────────────────────────────


def default_write_audit_dir() -> Path:
    """Return ``<hermes home>/logs/write_audit`` — the on-disk home of the log."""
    return get_hermes_home() / "logs" / "write_audit"


def month_file_for(moment: datetime, log_dir: Path) -> Path:
    """Return the ``YYYY-MM.jsonl`` file that *moment* belongs in."""
    return log_dir / f"{moment.astimezone(timezone.utc):%Y-%m}.jsonl"


def format_timestamp(moment: datetime) -> str:
    """Format as ISO8601 UTC with millisecond precision, e.g. ``2026-08-21T22:15:03.412Z``."""
    utc = moment.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── The append primitive ─────────────────────────────────────────────────────


def append_entry(
    entry: dict[str, Any],
    *,
    log_dir: Path,
    moment: datetime,
) -> Path:
    """Append one JSON object as a line to the month file, fsync'd before returning.

    The file is opened in append mode only, created ``0600``, and never shortened
    or rewritten.  Serialization happens before the file is touched so a
    non-serializable payload cannot leave a partial line behind.

    When this call is what brings a new month file into existence, the containing
    directory is fsync'd too — otherwise the line's bytes would be durable while
    the directory entry pointing at them was not, and a crash could lose the very
    first line of a month after its destination write had already happened.

    Raises ``OSError``/``TypeError``/``ValueError`` on failure — callers wrap those
    into :class:`WriteAuditLogError` with the blocked-write context attached.
    """
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False) + "\n"

    log_dir.mkdir(parents=True, exist_ok=True)
    path = month_file_for(moment, log_dir)
    is_new_file = not path.exists()

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        handle = open(fd, "a", encoding="utf-8", closefd=True)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())

    if is_new_file:
        _fsync_directory(log_dir)
    return path


def _fsync_directory(directory: Path) -> None:
    """Make a newly created file's directory entry durable."""
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ── Entry construction ───────────────────────────────────────────────────────


def build_entry(
    *,
    timestamp: str,
    destination: str,
    record_id: Any,
    operation: str,
    before: Any,
    after: Any,
    actor: str,
    trigger: str,
    audit_id: str,
    audit_phase: str,
    after_fetch_failed: bool = False,
) -> dict[str, Any]:
    """Build one log entry dict with the required fields in a stable order."""
    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "destination": destination,
        "record_id": record_id,
        "operation": operation,
        "before": before,
        "after": after,
        "actor": actor,
        "trigger": trigger,
        "audit_id": audit_id,
        "audit_phase": audit_phase,
    }
    if after_fetch_failed:
        entry["after_fetch_failed"] = True
    return entry


# ── The gate ─────────────────────────────────────────────────────────────────


@dataclass
class AuthorizedWrite:
    """Proof that an intent line for one write is durably on disk.

    A client obtains one of these from :meth:`WriteAuditRecorder.authorize_write`
    and only then calls the destination API.  Holding this object is what makes
    the destination call legal; there is no other way to reach it in the write
    clients built on this module.
    """

    recorder: "WriteAuditRecorder"
    audit_id: str
    operation: str
    record_id: Any
    before: Any
    trigger: str
    intent_path: Path

    def record_outcome(
        self,
        *,
        record_id: Any = None,
        after: Any = None,
        after_fetch_failed: bool = False,
    ) -> Path:
        """Append the outcome line after the destination call returned successfully.

        Pass ``record_id`` to fill in an ID that only existed after a create.  When
        *after_fetch_failed* is true, ``after`` is forced to :data:`AFTER_UNKNOWN`
        so the "we wrote it but could not read it back" case is unambiguous.
        """
        resolved_id = self.record_id if record_id is None else record_id
        resolved_after = AFTER_UNKNOWN if after_fetch_failed else after
        moment = self.recorder.now()
        entry = build_entry(
            timestamp=format_timestamp(moment),
            destination=self.recorder.destination,
            record_id=resolved_id,
            operation=self.operation,
            before=self.before,
            after=resolved_after,
            actor=self.recorder.actor,
            trigger=self.trigger,
            audit_id=self.audit_id,
            audit_phase=PHASE_OUTCOME,
            after_fetch_failed=after_fetch_failed,
        )
        try:
            return append_entry(entry, log_dir=self.recorder.log_dir, moment=moment)
        except Exception as exc:
            raise WriteAuditOutcomeLogError(
                f"The {self.recorder.destination} {self.operation} of record "
                f"{resolved_id!r} SUCCEEDED at the destination, but its outcome audit "
                f"line could not be appended to "
                f"{month_file_for(moment, self.recorder.log_dir)}: {exc!r}. "
                f"DO NOT RETRY THE WRITE — it already happened. The intent line "
                f"(audit_id={self.audit_id}) is on disk; the post-write state is not. "
                f"Trigger was: {self.trigger!r}."
            ) from exc


@dataclass
class WriteAuditRecorder:
    """Gates writes to one destination behind a durable audit append.

    *log_dir* defaults to :func:`default_write_audit_dir`.  *clock* is injectable
    so tests can place entries in specific months.
    """

    destination: str
    actor: str
    log_dir: Path | None = None
    clock: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        if self.destination not in KNOWN_DESTINATIONS:
            raise ValueError(
                f"Unknown write-audit destination {self.destination!r}; "
                f"expected one of {', '.join(KNOWN_DESTINATIONS)}."
            )
        if not self.actor or not str(self.actor).strip():
            raise ValueError("A write-audit recorder needs a non-empty actor name.")
        self.log_dir = Path(self.log_dir) if self.log_dir is not None else default_write_audit_dir()

    def now(self) -> datetime:
        return (self.clock or _utc_now)()

    def authorize_write(
        self,
        *,
        operation: str,
        record_id: Any,
        before: Any,
        trigger: str,
    ) -> AuthorizedWrite:
        """Append the intent line and return only once it is fsync'd to disk.

        **Call the destination API only after this returns.**  Any failure raises
        :class:`WriteAuditLogError`, which propagates to the caller unmodified —
        never swallowed, never downgraded to a warning or a falsy return value.
        """
        validated_trigger = require_trigger(trigger)
        if operation not in KNOWN_OPERATIONS:
            raise ValueError(
                f"Unknown write operation {operation!r}; expected one of "
                f"{', '.join(KNOWN_OPERATIONS)}. No audit line was written and no "
                f"destination API was called."
            )

        audit_id = uuid.uuid4().hex
        moment = self.now()
        entry = build_entry(
            timestamp=format_timestamp(moment),
            destination=self.destination,
            record_id=record_id,
            operation=operation,
            before=before,
            after=None,
            actor=self.actor,
            trigger=validated_trigger,
            audit_id=audit_id,
            audit_phase=PHASE_INTENT,
        )

        assert self.log_dir is not None  # set in __post_init__
        try:
            intent_path = append_entry(entry, log_dir=self.log_dir, moment=moment)
        except Exception as exc:
            raise WriteAuditLogError(
                f"BLOCKED: the {self.destination} {operation} of record {record_id!r} was "
                f"NOT attempted — the destination API was never called — because its "
                f"write-audit line could not be appended to "
                f"{month_file_for(moment, self.log_dir)}: {exc!r}. "
                f"Actor: {self.actor}. Trigger: {validated_trigger!r}. "
                f"Fix the log path/permissions and re-run the write."
            ) from exc

        return AuthorizedWrite(
            recorder=self,
            audit_id=audit_id,
            operation=operation,
            record_id=record_id,
            before=before,
            trigger=validated_trigger,
            intent_path=intent_path,
        )


# ── Read side (used by scripts/write_audit_query.py) ─────────────────────────


def month_files(log_dir: Path) -> list[Path]:
    """Return the ``YYYY-MM.jsonl`` files under *log_dir*, oldest name first."""
    if not log_dir.is_dir():
        return []
    return sorted(p for p in log_dir.glob("*.jsonl") if p.is_file())


def iter_entries(log_dir: Path) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    """Yield ``(path, line_number, entry)`` for every parseable line, read-only.

    Opened in read mode; this side of the module never writes.  Unparseable lines
    are skipped rather than raising, so one corrupt line cannot hide the rest of
    the history.
    """
    for path in month_files(log_dir):
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield path, line_number, parsed
