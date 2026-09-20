"""Desk CallHistory integrity checker.

Proof tooling only. This module is never imported by plugin registration
(`plugins/team_duncan_contacts/__init__.py`), agent-facing tools
(`tools.py`), or the runtime ingestion path (`ingestion_runner.py`,
`call_history_collector.py`). It exists so an external, separately
authorized proof harness can capture a before/after integrity snapshot of
the exact three CallHistory files around a single production scan, without
ever running that scan itself.

Shares its fixed SSH identity, target, hardening options, and subprocess
timeout with `call_history_collector` by importing them directly, so the
two can never drift apart. Every remote profile here is separate from, and
never merged with, the SQLite scan profile: two purpose-built sandbox
profiles (stat-only, openssl-only), neither of which can execute
`/usr/bin/sqlite3`, and the scan profile cannot execute `stat` or
`openssl`.

Uses only native `/usr/bin/stat` and `/usr/bin/openssl dgst -sha256 -r`
for measurement.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .call_history_collector import (
    _CALL_HISTORY_DB_PATH,
    _CALL_HISTORY_SHM_PATH,
    _CALL_HISTORY_WAL_PATH,
    _LIVE_SSH_IDENTITY,
    _LIVE_SSH_TARGET,
    _ROOT_BOOTSTRAP_LITERAL,
    _SSH_BIN,
    _SUBPROCESS_TIMEOUT_S,
)

_STAT_BIN = "/usr/bin/stat"
_OPENSSL_BIN = "/usr/bin/openssl"
_OPENSSL_CNF_PATH = "/private/etc/ssl/openssl.cnf"

#: Fixed SSH hardening, identical to the transport's.
_SSH_HARDENING_ARGS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
)

#: Exact, fixed order: CallHistory main, WAL, SHM. Never any other path.
_INTEGRITY_TARGET_PATHS: tuple[str, ...] = (
    _CALL_HISTORY_DB_PATH,
    _CALL_HISTORY_WAL_PATH,
    _CALL_HISTORY_SHM_PATH,
)

#: Basename -> exact full path, derived from the same literals the
#: production transport reads. Never a separately hardcoded basename.
_BASENAME_TO_PATH: dict[str, str] = {
    Path(p).name: p for p in _INTEGRITY_TARGET_PATHS
}

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class IntegrityCheckError(RuntimeError):
    """Raised for every integrity-checker failure path. The message is
    always a fixed classification string -- never stdout, stderr, remote
    command text, file content, or a hash/size/mtime value."""


def _stat_sandbox_profile() -> str:
    reads = " ".join(
        f'(literal "{p}")' for p in (_STAT_BIN, *_INTEGRITY_TARGET_PATHS)
    )
    return (
        "(version 1)(deny default)(deny file-write*)(deny network*)"
        f'(allow process-exec* (literal "{_STAT_BIN}"))'
        f'(allow file-read-data (literal "{_ROOT_BOOTSTRAP_LITERAL}"))'
        f"(allow file-read* {reads})"
    )


def _openssl_sandbox_profile() -> str:
    reads = " ".join(
        f'(literal "{p}")'
        for p in (_OPENSSL_BIN, _OPENSSL_CNF_PATH, *_INTEGRITY_TARGET_PATHS)
    )
    return (
        "(version 1)(deny default)(deny file-write*)(deny network*)"
        f'(allow process-exec* (literal "{_OPENSSL_BIN}"))'
        f'(allow file-read-data (literal "{_ROOT_BOOTSTRAP_LITERAL}"))'
        f"(allow file-read* {reads})"
    )


def _ssh_argv(ssh_identity: str, ssh_target: str, remote_cmd: str) -> list[str]:
    return [
        _SSH_BIN, "-i", ssh_identity,
        *_SSH_HARDENING_ARGS,
        ssh_target, remote_cmd,
    ]


def build_stat_command(*, ssh_identity: str, ssh_target: str) -> list[str]:
    """Pure command construction only -- never opens a socket or subprocess."""
    quoted_paths = " ".join(f"'{p}'" for p in _INTEGRITY_TARGET_PATHS)
    remote_cmd = (
        f"LC_ALL=C /usr/bin/sandbox-exec -p '{_stat_sandbox_profile()}' "
        f"{_STAT_BIN} -f '%N %z %m' {quoted_paths}"
    )
    return _ssh_argv(ssh_identity, ssh_target, remote_cmd)


def build_hash_command(*, ssh_identity: str, ssh_target: str) -> list[str]:
    """Pure command construction only -- never opens a socket or subprocess."""
    quoted_paths = " ".join(f"'{p}'" for p in _INTEGRITY_TARGET_PATHS)
    remote_cmd = (
        f"LC_ALL=C /usr/bin/sandbox-exec -p '{_openssl_sandbox_profile()}' "
        f"{_OPENSSL_BIN} dgst -sha256 -r {quoted_paths}"
    )
    return _ssh_argv(ssh_identity, ssh_target, remote_cmd)


def _run_ssh_subprocess(argv: list[str]) -> subprocess.CompletedProcess:
    """Default production runner: one-shot `argv` execution, never a shell."""
    return subprocess.run(
        argv,
        input=b"",
        capture_output=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _execute(
    argv: list[str], runner: Callable[[list[str]], subprocess.CompletedProcess]
) -> bytes:
    try:
        completed = runner(argv)
    except subprocess.TimeoutExpired:
        raise IntegrityCheckError("Desk integrity check timed out.") from None
    except Exception:
        raise IntegrityCheckError("Desk integrity check failed to execute.") from None

    if completed.returncode != 0:
        raise IntegrityCheckError(
            "Desk integrity check process exited abnormally."
        )
    return completed.stdout


@dataclass(frozen=True)
class StatRecord:
    path: str
    size_bytes: int
    mtime_epoch: int


def _parse_stat_output(stdout_bytes: bytes) -> tuple[StatRecord, ...]:
    """Strictly parse exactly three `stat -f '%N %z %m'` records, matched
    by exact expected basename. Rejects malformed, missing, extra,
    duplicate, wrong-path, wrong-type, and open-ended text."""
    try:
        text = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrityCheckError("Desk integrity stat output was malformed.") from None

    lines = [line for line in text.splitlines() if line != ""]
    if len(lines) != 3:
        raise IntegrityCheckError("Desk integrity stat output was malformed.")

    by_basename: dict[str, StatRecord] = {}
    for line in lines:
        parts = line.rsplit(None, 2)
        if len(parts) != 3:
            raise IntegrityCheckError("Desk integrity stat output was malformed.")
        name, size_str, mtime_str = parts
        if not size_str.isdigit() or not mtime_str.isdigit():
            raise IntegrityCheckError("Desk integrity stat output was malformed.")

        basename = name.rsplit("/", 1)[-1]
        expected_path = _BASENAME_TO_PATH.get(basename)
        if expected_path is None:
            raise IntegrityCheckError("Desk integrity stat output was malformed.")
        if basename in by_basename:
            raise IntegrityCheckError("Desk integrity stat output was malformed.")

        by_basename[basename] = StatRecord(
            path=expected_path, size_bytes=int(size_str), mtime_epoch=int(mtime_str)
        )

    if set(by_basename) != set(_BASENAME_TO_PATH):
        raise IntegrityCheckError("Desk integrity stat output was malformed.")

    return tuple(by_basename[b] for b in _BASENAME_TO_PATH)


@dataclass(frozen=True)
class HashRecord:
    path: str
    sha256_hex: str


def _parse_hash_output(stdout_bytes: bytes) -> tuple[HashRecord, ...]:
    """Strictly parse exactly three `openssl dgst -sha256 -r` records,
    matched by exact expected full path. Rejects malformed, missing,
    extra, duplicate, wrong-path, non-hex, wrong-length, and open-ended
    text."""
    try:
        text = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrityCheckError("Desk integrity hash output was malformed.") from None

    lines = [line for line in text.splitlines() if line != ""]
    if len(lines) != 3:
        raise IntegrityCheckError("Desk integrity hash output was malformed.")

    by_path: dict[str, HashRecord] = {}
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise IntegrityCheckError("Desk integrity hash output was malformed.")
        digest, tagged_name = parts
        if not _HEX64_RE.match(digest):
            raise IntegrityCheckError("Desk integrity hash output was malformed.")
        if not tagged_name.startswith("*"):
            raise IntegrityCheckError("Desk integrity hash output was malformed.")

        name = tagged_name[1:]
        if name not in _INTEGRITY_TARGET_PATHS:
            raise IntegrityCheckError("Desk integrity hash output was malformed.")
        if name in by_path:
            raise IntegrityCheckError("Desk integrity hash output was malformed.")

        by_path[name] = HashRecord(path=name, sha256_hex=digest)

    if set(by_path) != set(_INTEGRITY_TARGET_PATHS):
        raise IntegrityCheckError("Desk integrity hash output was malformed.")

    return tuple(by_path[p] for p in _INTEGRITY_TARGET_PATHS)


@dataclass(frozen=True)
class FileIntegritySnapshot:
    path: str
    size_bytes: int
    mtime_epoch: int
    sha256_hex: str


@dataclass(frozen=True)
class IntegritySnapshot:
    call_history: FileIntegritySnapshot
    wal: FileIntegritySnapshot
    shm: FileIntegritySnapshot


def capture_integrity_snapshot(
    *,
    stat_runner: Callable[[list[str]], subprocess.CompletedProcess] = _run_ssh_subprocess,
    hash_runner: Callable[[list[str]], subprocess.CompletedProcess] = _run_ssh_subprocess,
) -> IntegritySnapshot:
    """Read-only. Runs the stat profile, then the openssl profile, and
    returns a typed snapshot for exactly the three CallHistory files. Never
    runs the SQLite scan -- the parent proof orchestrates snapshot, one
    scan, snapshot, itself."""
    stat_argv = build_stat_command(ssh_identity=_LIVE_SSH_IDENTITY, ssh_target=_LIVE_SSH_TARGET)
    stat_records = _parse_stat_output(_execute(stat_argv, stat_runner))

    hash_argv = build_hash_command(ssh_identity=_LIVE_SSH_IDENTITY, ssh_target=_LIVE_SSH_TARGET)
    hash_records = _parse_hash_output(_execute(hash_argv, hash_runner))

    stat_by_path = {r.path: r for r in stat_records}
    hash_by_path = {r.path: r for r in hash_records}

    def _combine(path: str) -> FileIntegritySnapshot:
        s = stat_by_path[path]
        h = hash_by_path[path]
        return FileIntegritySnapshot(
            path=path,
            size_bytes=s.size_bytes,
            mtime_epoch=s.mtime_epoch,
            sha256_hex=h.sha256_hex,
        )

    return IntegritySnapshot(
        call_history=_combine(_CALL_HISTORY_DB_PATH),
        wal=_combine(_CALL_HISTORY_WAL_PATH),
        shm=_combine(_CALL_HISTORY_SHM_PATH),
    )


@dataclass(frozen=True)
class FileIntegrityComparison:
    size_equal: bool
    mtime_equal: bool
    sha256_equal: bool

    @property
    def all_equal(self) -> bool:
        return self.size_equal and self.mtime_equal and self.sha256_equal


@dataclass(frozen=True)
class IntegrityComparison:
    call_history: FileIntegrityComparison
    wal: FileIntegrityComparison
    shm: FileIntegrityComparison

    @property
    def all_equal(self) -> bool:
        return (
            self.call_history.all_equal
            and self.wal.all_equal
            and self.shm.all_equal
        )


def compare_integrity_snapshots(
    before: IntegritySnapshot, after: IntegritySnapshot
) -> IntegrityComparison:
    """Read-only comparison. Runs no command and touches no path."""

    def _compare(
        b: FileIntegritySnapshot, a: FileIntegritySnapshot
    ) -> FileIntegrityComparison:
        return FileIntegrityComparison(
            size_equal=b.size_bytes == a.size_bytes,
            mtime_equal=b.mtime_epoch == a.mtime_epoch,
            sha256_equal=b.sha256_hex == a.sha256_hex,
        )

    return IntegrityComparison(
        call_history=_compare(before.call_history, after.call_history),
        wal=_compare(before.wal, after.wal),
        shm=_compare(before.shm, after.shm),
    )
