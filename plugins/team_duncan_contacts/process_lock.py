"""OPS-114 durable process lock shared by every automated Team Duncan mode
(desk / plaud-reconcile / plaud-webhook) and by the interactive
confirm_call_log_ingest / accept_call_log_ingest_run / confirm_plaud_summary_run
execution paths -- the exact set of handlers that mutate ingestion_state.db.

Held with kernel ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a single durable lock
file under ``<hermes_home>/cron/locks/``, never ``/tmp``. The kernel is the
proof that a dead owner can never block forever: an flock is attached to an
open file description, and the kernel unconditionally releases every flock
a process holds when that process's file descriptors are torn down, even on
a hard crash or SIGKILL with no cleanup code ever running. There is no
window between "reserved" and "fully recorded" the way a two-step
mkdir-then-write-metadata sequence has, and no PID/age heuristic decides
whether the lock is free -- the kernel already decided that the moment the
holder's process ended. A lock file can carry arbitrary leftover PID/mode
metadata from a long-dead owner and still be acquired immediately, because
that metadata is never consulted to make the acquire/deny decision; it
exists only so an operator can inspect who last held the lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOCK_FILE_NAME = "team-duncan-automation.lock"


@dataclass
class LockResult:
    acquired: bool
    owner_pid: int | None = None
    stale_recovered: bool = False


class TeamDuncanLock:
    """One durable lock file per Hermes home. Not reentrant: a second
    ``acquire()`` on an already-acquired instance is a no-op collision with
    itself unless ``release()`` is called first."""

    def __init__(self, *, lock_dir: Path | None = None, hermes_home: Path | None = None) -> None:
        if lock_dir is not None:
            self._lock_path = Path(lock_dir)
        else:
            from hermes_constants import get_hermes_home

            base = Path(hermes_home) if hermes_home is not None else Path(get_hermes_home())
            self._lock_path = base / "cron" / "locks" / _LOCK_FILE_NAME
        self._fd: int | None = None
        self._acquired = False

    @property
    def lock_dir(self) -> Path:
        """The lock file's path (named for historical/call-site continuity;
        it is a single file, not a directory)."""
        return self._lock_path

    def acquire(self, *, mode: str) -> LockResult:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        preexisting_metadata = self._lock_path.is_file() and self._lock_path.stat().st_size > 0
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner_pid = self._read_owner_pid(fd)
            os.close(fd)
            return LockResult(acquired=False, owner_pid=owner_pid)

        self._write_meta(fd, mode)
        self._fd = fd
        self._acquired = True
        return LockResult(
            acquired=True, owner_pid=os.getpid(), stale_recovered=preexisting_metadata
        )

    def release(self) -> None:
        if not self._acquired or self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        self._acquired = False

    def __enter__(self) -> "TeamDuncanLock":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.release()

    # --- internals ----------------------------------------------------------

    def _write_meta(self, fd: int, mode: str) -> None:
        payload = json.dumps(
            {"pid": os.getpid(), "mode": mode, "acquired_at": time.time()}, sort_keys=True
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)

    def _read_owner_pid(self, fd: int) -> int | None:
        """Best-effort inspection only (for logging/debugging) -- never
        consulted to decide whether the lock is free. Missing or unreadable
        metadata (including the crash window between open() and the
        metadata write) simply reads back as an unknown owner."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid")
        try:
            return int(pid) if pid is not None else None
        except (TypeError, ValueError):
            return None


__all__ = ["TeamDuncanLock", "LockResult"]
