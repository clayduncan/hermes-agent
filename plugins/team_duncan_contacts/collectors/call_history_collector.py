"""Desk CallHistory collector.

Read-only, deny-default, fixed-path sandboxed access to the Desk's Messages
CallHistory database over SSH. Command construction lives in this module and
is fully unit-testable without a network; the production transport that
actually opens an SSH connection is defined here too (a production-capable
adapter is allowed), but nothing in this module makes that connection on
import, at rest, or from anything this build's tests exercise -- tests only
ever inject `FakeDeskTransport`.

Sandbox design (fixed, not a starting point -- widening it requires a new,
separately authorized plan):
  * `/usr/bin/sqlite3 -readonly -batch` against `file:...?mode=ro`.
    Never Python's sqlite3 module against a Desk path. `-uri` is not
    supported on the current Desk sqlite3 build and is never passed.
  * Wrapped in `/usr/bin/sandbox-exec` under `(deny default)(deny
    file-write*)(deny network*)`, with the exact CallHistory main/WAL/SHM
    and `/usr/bin/sqlite3` file-read literals plus a single root-inode
    `(allow file-read-data (literal "/"))` bootstrap rule (data read on the
    root directory inode only, not any child path -- required for native
    Mach-O process bootstrap; it grants no subpath content), and a single
    allowed executable (`/usr/bin/sqlite3`). No mach-lookup, no
    sysctl-read, no other process.
  * The fixed `.sql` script (never templated -- see the three `.sql` files
    beside this module) is piped fresh over the SSH connection's stdin for
    every invocation. It is never written to any path on the Desk.
  * Every dynamic value is bound via the sqlite3 CLI's own
    `.parameter set` dot-command, never SQL-text concatenation or shell
    interpolation, and must already be a Python int/float (never bool)
    before a command is built.
"""

from __future__ import annotations

import hmac as _hmac_mod
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..ingestion_state_db import SOURCE_DESK_CALL

#: Hardcoded. Not configurable via argument, environment variable, or
#: runtime option -- widening this requires a new, separately authorized plan.
CALL_HISTORY_LOOKBACK_DAYS = 7

#: Small fixed tolerance band absorbing minor source-side duration rounding.
_DURATION_TOLERANCE_SECONDS = 2

#: Small fixed tolerance band (1ms) absorbing float/IEEE-754 and
#: datetime-microsecond round-trip drift on ZDATE (e.g. converting an
#: Apple-epoch float to an ISO-microsecond timestamp and back). Not a
#: call-level heuristic window -- far narrower than any real gap between
#: distinct calls. `fetch_exact_event` still verifies the returned row's
#: recomputed source_event_id exactly, so this only recovers numeric
#: representation drift; it never selects among multiple distinct calls.
_ZDATE_TOLERANCE_SECONDS = 0.001

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_CALL_HISTORY_DB_PATH = (
    "/Users/clayduncan/Library/Application Support/CallHistoryDB/CallHistory.storedata"
)
_CALL_HISTORY_WAL_PATH = _CALL_HISTORY_DB_PATH + "-wal"
_CALL_HISTORY_SHM_PATH = _CALL_HISTORY_DB_PATH + "-shm"
_SQLITE3_BIN = "/usr/bin/sqlite3"
_SANDBOX_EXEC_BIN = "/usr/bin/sandbox-exec"
_SSH_BIN = "/usr/bin/ssh"

#: Root-inode bootstrap rule required for native Mach-O process bootstrap
#: on current macOS. Grants file-read-data on the root directory inode
#: only -- not a subpath, not a regex, no child-path content. This is not
#: an OS-version-specific literal: unlike a dyld shared cache path (which
#: has moved under a per-OS Cryptex layout and is no longer a single
#: literal file at all), the root inode itself is not versioned.
_ROOT_BOOTSTRAP_LITERAL = "/"

#: The fixed, exact, four-entry file-read allowlist. Final, never widened
#: from inside this module.
SANDBOX_FILE_READ_ALLOWLIST: tuple[str, ...] = (
    _CALL_HISTORY_DB_PATH,
    _CALL_HISTORY_WAL_PATH,
    _CALL_HISTORY_SHM_PATH,
    _SQLITE3_BIN,
)

_SQL_DIR = Path(__file__).resolve().parent
_ROUTINE_SQL_PATH = _SQL_DIR / "read_call_history_routine.sql"
_REPLAY_WITH_DURATION_SQL_PATH = _SQL_DIR / "read_call_history_replay_with_duration.sql"
_REPLAY_NO_DURATION_SQL_PATH = _SQL_DIR / "read_call_history_replay_no_duration.sql"


def _sandbox_profile() -> str:
    reads = " ".join(f'(literal "{p}")' for p in SANDBOX_FILE_READ_ALLOWLIST)
    return (
        "(version 1)(deny default)(deny file-write*)(deny network*)"
        f'(allow process-exec* (literal "{_SQLITE3_BIN}"))'
        f'(allow file-read-data (literal "{_ROOT_BOOTSTRAP_LITERAL}"))'
        f"(allow file-read* {reads})"
    )


def apple_epoch_to_utc(zdate: float) -> datetime:
    return APPLE_EPOCH + timedelta(seconds=zdate)


def utc_to_apple_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - APPLE_EPOCH).total_seconds()


def routine_cutoff_apple_epoch(now: datetime) -> float:
    """Inclusive lower bound for the fixed 7-day routine window. No upper
    bound is applied -- the window extends through "now" implicitly."""
    cutoff = now - timedelta(days=CALL_HISTORY_LOOKBACK_DAYS)
    return utc_to_apple_epoch(cutoff)


def _validate_numeric(name: str, value: Any) -> None:
    """Every dynamic value must be an int/float (explicitly not bool) before
    it's allowed anywhere near a `.parameter set` line."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} must be an int or float (never bool); got {type(value).__name__}."
        )


def compute_desk_source_event_id(
    identity_key: bytes, zdate: float, zaddress: str, zduration: float | None
) -> str:
    """Deterministic, stable, non-reversible event identity for a Desk call row.

    Keyed with a dedicated ingestion identity key (see
    ingestion_state_db.load_or_create_identity_key) -- independent from the
    registry's contact-matching key -- so this identifier cannot be brute-
    forced back to the caller's phone number the way an unsalted hash of a
    ~10-digit number space could be.
    """
    msg = f"desk_call|{zdate!r}|{zaddress}|{zduration!r}"
    return _hmac_mod.new(identity_key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def build_routine_command(
    *, ssh_identity: str, ssh_target: str, cutoff_apple_epoch: float
) -> tuple[list[str], bytes]:
    """Return (argv, stdin_bytes) for the fixed 7-day routine scan.

    Pure command construction only -- this function never opens a socket or
    a subprocess.
    """
    _validate_numeric("cutoff_apple_epoch", cutoff_apple_epoch)
    remote_cmd = (
        f"LC_ALL=C {_SANDBOX_EXEC_BIN} -p '{_sandbox_profile()}' "
        f"{_SQLITE3_BIN} -readonly -batch "
        f"'file:{_CALL_HISTORY_DB_PATH}?mode=ro'"
    )
    argv = [
        _SSH_BIN, "-i", ssh_identity,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        ssh_target, remote_cmd,
    ]
    stdin = (
        ".mode json\n"
        f".parameter set :cutoff_apple_epoch {cutoff_apple_epoch!r}\n"
        + _ROUTINE_SQL_PATH.read_text(encoding="utf-8")
    ).encode("utf-8")
    return argv, stdin


def build_replay_command(
    *,
    ssh_identity: str,
    ssh_target: str,
    target_zdate: float,
    zoriginated: int,
    zanswered: int,
    duration_s: float | None,
) -> tuple[list[str], bytes]:
    """Return (argv, stdin_bytes) for the exact-event point lookup.

    Selects the with-duration or no-duration script based on whether
    *duration_s* is known -- never hardcodes zanswered=1 for the no-duration
    (unanswered-call) variant.
    """
    _validate_numeric("target_zdate", target_zdate)
    _validate_numeric("zoriginated", zoriginated)
    _validate_numeric("zanswered", zanswered)

    zdate_low = target_zdate - _ZDATE_TOLERANCE_SECONDS
    zdate_high = target_zdate + _ZDATE_TOLERANCE_SECONDS

    remote_cmd = (
        f"LC_ALL=C {_SANDBOX_EXEC_BIN} -p '{_sandbox_profile()}' "
        f"{_SQLITE3_BIN} -readonly -batch "
        f"'file:{_CALL_HISTORY_DB_PATH}?mode=ro'"
    )
    argv = [
        _SSH_BIN, "-i", ssh_identity,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        ssh_target, remote_cmd,
    ]

    if duration_s is None:
        stdin_text = (
            ".mode json\n"
            f".parameter set :zdate_low {zdate_low!r}\n"
            f".parameter set :zdate_high {zdate_high!r}\n"
            f".parameter set :zoriginated {int(zoriginated)!r}\n"
            f".parameter set :zanswered {int(zanswered)!r}\n"
            + _REPLAY_NO_DURATION_SQL_PATH.read_text(encoding="utf-8")
        )
    else:
        _validate_numeric("duration_s", duration_s)
        duration_low = duration_s - _DURATION_TOLERANCE_SECONDS
        duration_high = duration_s + _DURATION_TOLERANCE_SECONDS
        stdin_text = (
            ".mode json\n"
            f".parameter set :zdate_low {zdate_low!r}\n"
            f".parameter set :zdate_high {zdate_high!r}\n"
            f".parameter set :duration_low {duration_low!r}\n"
            f".parameter set :duration_high {duration_high!r}\n"
            f".parameter set :zoriginated {int(zoriginated)!r}\n"
            f".parameter set :zanswered {int(zanswered)!r}\n"
            + _REPLAY_WITH_DURATION_SQL_PATH.read_text(encoding="utf-8")
        )
    return argv, stdin_text.encode("utf-8")


class DeskTransport(Protocol):
    """Injectable transport boundary. Tests inject `FakeDeskTransport`;
    nothing in this build's own test suite constructs a transport that
    reaches a real Desk, network, or credential."""

    def run_routine_scan(self, now: datetime) -> list[dict[str, Any]]:
        """Return raw rows (ZDATE, ZADDRESS, ZDURATION, ZORIGINATED,
        ZANSWERED) for the fixed 7-day window ending at *now*."""
        ...

    def run_replay_lookup(
        self,
        *,
        target_zdate: float,
        zoriginated: int,
        zanswered: int,
        duration_s: float | None,
    ) -> list[dict[str, Any]]:
        """Return raw rows matching the exact-event point lookup."""
        ...


class DeskTransportError(RuntimeError):
    """Raised for every `LiveDeskTransport` failure path. The message is
    always a fixed classification string -- never stdout, stderr, remote
    command text, SQL, row content, or credentials."""


#: Fixed production identity/target. Not configurable via argument,
#: environment variable, or runtime option -- widening this requires a new,
#: separately authorized plan.
_LIVE_SSH_IDENTITY = "/Users/claysystemshq/.ssh/desk_deploy"
_LIVE_SSH_TARGET = "clayduncan@100.115.84.20"

#: Fixed subprocess timeout, in seconds. Not configurable.
_SUBPROCESS_TIMEOUT_S = 120

#: Fixed maximum row count enforced before any normalization.
_MAX_DESK_ROWS = 10_000

#: The exact five columns every row must contain, no more, no less.
_EXPECTED_ROW_FIELDS = frozenset({"ZDATE", "ZADDRESS", "ZDURATION", "ZORIGINATED", "ZANSWERED"})


def _run_ssh_subprocess(argv: list[str], stdin_bytes: bytes) -> subprocess.CompletedProcess:
    """Default production runner: one-shot `argv` execution, never a shell.
    The fixed SQL script travels only through `input=`, never a file, and
    never touches the Desk's disk."""
    return subprocess.run(
        argv,
        input=stdin_bytes,
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _validate_desk_row_shape(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise DeskTransportError("Desk transport returned a malformed row.")
    if set(item.keys()) != _EXPECTED_ROW_FIELDS:
        raise DeskTransportError("Desk transport returned a malformed row.")

    zdate = item["ZDATE"]
    if isinstance(zdate, bool) or not isinstance(zdate, (int, float)):
        raise DeskTransportError("Desk transport returned a malformed row.")

    zaddress = item["ZADDRESS"]
    if not isinstance(zaddress, str):
        raise DeskTransportError("Desk transport returned a malformed row.")

    for key in ("ZDURATION", "ZORIGINATED", "ZANSWERED"):
        value = item[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise DeskTransportError("Desk transport returned a malformed row.")

    return item


def _parse_desk_rows(stdout_bytes: bytes) -> list[dict[str, Any]]:
    """Parse fixed `.mode json` sqlite3 stdout into a validated row list.
    Never falls back to ad hoc delimiter parsing."""
    stripped = stdout_bytes.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except (ValueError, UnicodeDecodeError):
        raise DeskTransportError("Desk transport returned unparseable output.") from None

    if not isinstance(parsed, list):
        raise DeskTransportError("Desk transport returned a malformed result.")
    if len(parsed) > _MAX_DESK_ROWS:
        raise DeskTransportError("Desk transport returned an oversized result.")

    return [_validate_desk_row_shape(item) for item in parsed]


class LiveDeskTransport:
    """Production `DeskTransport`: one-shot SSH subprocess per read against
    the fixed Team Duncan Desk, no ControlMaster, no PTY. Identity, target,
    and every hardening option are fixed constants, never configurable via
    argument, environment variable, or runtime option. *runner* is the only
    injectable seam -- tests supply a fake so this build's own test suite
    never spawns a real process."""

    def __init__(
        self,
        *,
        runner: Callable[[list[str], bytes], subprocess.CompletedProcess] = _run_ssh_subprocess,
    ) -> None:
        self._runner = runner

    def run_routine_scan(self, now: datetime) -> list[dict[str, Any]]:
        cutoff = routine_cutoff_apple_epoch(now)
        argv, stdin = build_routine_command(
            ssh_identity=_LIVE_SSH_IDENTITY,
            ssh_target=_LIVE_SSH_TARGET,
            cutoff_apple_epoch=cutoff,
        )
        return self._execute(argv, stdin)

    def run_replay_lookup(
        self,
        *,
        target_zdate: float,
        zoriginated: int,
        zanswered: int,
        duration_s: float | None,
    ) -> list[dict[str, Any]]:
        argv, stdin = build_replay_command(
            ssh_identity=_LIVE_SSH_IDENTITY,
            ssh_target=_LIVE_SSH_TARGET,
            target_zdate=target_zdate,
            zoriginated=zoriginated,
            zanswered=zanswered,
            duration_s=duration_s,
        )
        return self._execute(argv, stdin)

    def _execute(self, argv: list[str], stdin_bytes: bytes) -> list[dict[str, Any]]:
        try:
            completed = self._runner(argv, stdin_bytes)
        except subprocess.TimeoutExpired:
            raise DeskTransportError("Desk transport timed out.") from None
        except Exception:
            raise DeskTransportError("Desk transport failed to execute.") from None

        if completed.returncode != 0:
            raise DeskTransportError("Desk transport process exited abnormally.")

        return _parse_desk_rows(completed.stdout)


@dataclass
class DeskCallRecord:
    source: str
    source_event_id: str
    occurred_at: datetime
    duration_s: int | None
    raw_handle: str  # ZADDRESS; caller must clear it after resolve_event
    direction: str | None  # 'outbound' | 'inbound'
    answered: int | None

    @property
    def position(self) -> str:
        return self.occurred_at.isoformat()

    def provenance(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "direction": self.direction,
            "answered": self.answered,
        }


def _row_to_record(identity_key: bytes, row: dict[str, Any]) -> DeskCallRecord:
    zdate = row["ZDATE"]
    zaddress = row["ZADDRESS"]
    zduration = row.get("ZDURATION")
    zoriginated = row.get("ZORIGINATED")
    zanswered = row.get("ZANSWERED")

    source_event_id = compute_desk_source_event_id(identity_key, zdate, zaddress, zduration)
    direction = None
    if zoriginated is not None:
        direction = "outbound" if zoriginated else "inbound"

    return DeskCallRecord(
        source=SOURCE_DESK_CALL,
        source_event_id=source_event_id,
        occurred_at=apple_epoch_to_utc(zdate),
        duration_s=int(zduration) if zduration is not None else None,
        raw_handle=str(zaddress),
        direction=direction,
        answered=int(bool(zanswered)) if zanswered is not None else None,
    )


class CallHistoryCollector:
    """Normalizes Desk CallHistory rows via an injected DeskTransport."""

    def __init__(self, transport: DeskTransport, identity_key: bytes) -> None:
        self._transport = transport
        self._identity_key = identity_key

    def fetch_routine_window(self, now: datetime) -> list[DeskCallRecord]:
        rows = self._transport.run_routine_scan(now)
        return [_row_to_record(self._identity_key, r) for r in rows]

    def fetch_exact_event(
        self,
        *,
        target_zdate: float,
        zoriginated: int,
        zanswered: int,
        duration_s: float | None,
        expected_source_event_id: str,
    ) -> DeskCallRecord | None:
        """Sealed exact-source re-fetch. Recomputes source_event_id from the
        returned row(s) and only accepts an exact hash match -- any
        non-matching row is discarded in-memory immediately, never logged
        or persisted. Returns None on zero matches; raises AmbiguousReplayError
        on more than one match, since both are distinct, visible outcomes the
        caller (ingestion_runner) must route to source_missing/source_ambiguous."""
        rows = self._transport.run_replay_lookup(
            target_zdate=target_zdate,
            zoriginated=zoriginated,
            zanswered=zanswered,
            duration_s=duration_s,
        )
        matches = []
        for row in rows:
            record = _row_to_record(self._identity_key, row)
            if record.source_event_id == expected_source_event_id:
                matches.append(record)
        if len(matches) == 0:
            return None
        if len(matches) > 1:
            raise AmbiguousReplayError(
                f"Desk replay lookup found {len(matches)} rows matching the "
                "same source_event_id; ambiguous."
            )
        return matches[0]


class AmbiguousReplayError(RuntimeError):
    pass
