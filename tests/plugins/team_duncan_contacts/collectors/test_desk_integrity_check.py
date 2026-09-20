"""Tests for the Desk integrity checker (proof tooling only).

All tests use fake subprocess runners and in-memory fixture text -- no SSH,
no sandbox-exec, no real Desk, no live file access.
"""

from __future__ import annotations

import inspect
import subprocess

import pytest

import plugins.team_duncan_contacts as plugin_package
import plugins.team_duncan_contacts.ingestion_runner as ingestion_runner_module
import plugins.team_duncan_contacts.tools as tools_module
from plugins.team_duncan_contacts.collectors import call_history_collector as chc_module
from plugins.team_duncan_contacts.collectors import desk_integrity_check as dic_module
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    _CALL_HISTORY_DB_PATH,
    _CALL_HISTORY_SHM_PATH,
    _CALL_HISTORY_WAL_PATH,
)
from plugins.team_duncan_contacts.collectors.desk_integrity_check import (
    FileIntegritySnapshot,
    IntegrityCheckError,
    IntegritySnapshot,
    _openssl_sandbox_profile,
    _parse_hash_output,
    _parse_stat_output,
    _stat_sandbox_profile,
    build_hash_command,
    build_stat_command,
    capture_integrity_snapshot,
    compare_integrity_snapshots,
)

GOOD_SIZE = 4096
GOOD_MTIME = 1_700_000_000
GOOD_HASH_DB = "a" * 64
GOOD_HASH_WAL = "b" * 64
GOOD_HASH_SHM = "c" * 64


def _completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)


def _good_stat_stdout() -> bytes:
    lines = [
        f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME}",
        f"{_CALL_HISTORY_WAL_PATH} {GOOD_SIZE + 1} {GOOD_MTIME + 1}",
        f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE + 2} {GOOD_MTIME + 2}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _good_hash_stdout() -> bytes:
    lines = [
        f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH}",
        f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}",
        f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


# --- Structural: proof tooling only, never wired into plugin/runtime -------


def test_desk_integrity_check_not_imported_by_plugin_or_runtime() -> None:
    for module in (
        plugin_package,
        tools_module,
        ingestion_runner_module,
        chc_module,
    ):
        source = inspect.getsource(module)
        assert "desk_integrity_check" not in source


def test_shell_true_is_structurally_unreachable() -> None:
    source = inspect.getsource(dic_module)
    assert "shell=True" not in source
    assert "shell = True" not in source


# --- Stat profile: separate, exact, deny write/network, stat-only ----------


def test_stat_sandbox_profile_is_separate_and_exact() -> None:
    profile = _stat_sandbox_profile()
    assert "(deny default)" in profile
    assert "(deny file-write*)" in profile
    assert "(deny network*)" in profile
    assert profile.count("(allow process-exec*") == 1
    assert '(allow process-exec* (literal "/usr/bin/stat"))' in profile
    assert "/usr/bin/sqlite3" not in profile
    assert "/usr/bin/openssl" not in profile
    assert profile.count('(allow file-read-data (literal "/"))') == 1
    for path in (_CALL_HISTORY_DB_PATH, _CALL_HISTORY_WAL_PATH, _CALL_HISTORY_SHM_PATH):
        assert path in profile


def test_stat_command_uses_absolute_ssh_and_sandbox_exec_and_stat_only() -> None:
    argv = build_stat_command(ssh_identity="i", ssh_target="t")
    assert argv[0] == "/usr/bin/ssh"
    joined = " ".join(argv)
    assert "/usr/bin/sandbox-exec" in joined
    assert "/usr/bin/stat" in joined
    assert "/usr/bin/sqlite3" not in joined
    assert "/usr/bin/openssl" not in joined


# --- OpenSSL profile: separate, exact, deny write/network, openssl-only ----


def test_openssl_sandbox_profile_is_separate_and_exact() -> None:
    profile = _openssl_sandbox_profile()
    assert "(deny default)" in profile
    assert "(deny file-write*)" in profile
    assert "(deny network*)" in profile
    assert profile.count("(allow process-exec*") == 1
    assert '(allow process-exec* (literal "/usr/bin/openssl"))' in profile
    assert "/usr/bin/sqlite3" not in profile
    assert "/usr/bin/stat" not in profile
    assert profile.count('(allow file-read-data (literal "/"))') == 1
    assert "/private/etc/ssl/openssl.cnf" in profile
    for path in (_CALL_HISTORY_DB_PATH, _CALL_HISTORY_WAL_PATH, _CALL_HISTORY_SHM_PATH):
        assert path in profile


def test_hash_command_uses_absolute_ssh_and_sandbox_exec_and_openssl_only() -> None:
    argv = build_hash_command(ssh_identity="i", ssh_target="t")
    assert argv[0] == "/usr/bin/ssh"
    joined = " ".join(argv)
    assert "/usr/bin/sandbox-exec" in joined
    assert "/usr/bin/openssl" in joined
    assert "dgst" in joined
    assert "-sha256" in joined
    assert "/usr/bin/sqlite3" not in joined
    assert "/usr/bin/stat" not in joined


# --- No Perl, versioned runtime path, or shasum anywhere -------------------


def test_no_perl_shasum_or_versioned_runtime_path_in_module() -> None:
    source = inspect.getsource(dic_module)
    for banned in ("shasum", "Perl", "perl5", "/usr/bin/perl", "/System/Library/Perl"):
        assert banned not in source


def test_no_dynamic_allowlist_or_home_subpath_read() -> None:
    for profile in (_stat_sandbox_profile(), _openssl_sandbox_profile()):
        assert "(subpath" not in profile
        assert "(regex" not in profile
        assert "/Users\"" not in profile  # no bare-home subpath grant


# --- Strict stat parser -----------------------------------------------------


def test_stat_parser_accepts_exact_three_records() -> None:
    records = _parse_stat_output(_good_stat_stdout())
    assert len(records) == 3
    by_path = {r.path: r for r in records}
    assert by_path[_CALL_HISTORY_DB_PATH].size_bytes == GOOD_SIZE
    assert by_path[_CALL_HISTORY_DB_PATH].mtime_epoch == GOOD_MTIME
    assert by_path[_CALL_HISTORY_WAL_PATH].size_bytes == GOOD_SIZE + 1
    assert by_path[_CALL_HISTORY_SHM_PATH].mtime_epoch == GOOD_MTIME + 2


@pytest.mark.parametrize(
    "bad_stdout",
    [
        b"",
        b"not even close to a stat record",
        (f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME}\n").encode(),  # missing 2
        (
            f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_WAL_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"/extra/unexpected/path 1 1\n"
        ).encode(),  # extra record
        (
            f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
        ).encode(),  # duplicate, missing wal
        (
            f"/etc/passwd {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_WAL_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
        ).encode(),  # wrong path
        (
            f"{_CALL_HISTORY_DB_PATH} not-a-number {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_WAL_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
        ).encode(),  # wrong type
        (
            f"{_CALL_HISTORY_DB_PATH} {GOOD_SIZE} {GOOD_MTIME} unexpected trailing free text\n"
            f"{_CALL_HISTORY_WAL_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
            f"{_CALL_HISTORY_SHM_PATH} {GOOD_SIZE} {GOOD_MTIME}\n"
        ).encode(),  # open-ended text tacked onto a record
    ],
)
def test_stat_parser_rejects_bad_output(bad_stdout: bytes) -> None:
    with pytest.raises(IntegrityCheckError):
        _parse_stat_output(bad_stdout)


# --- Strict hash parser ------------------------------------------------------


def test_hash_parser_accepts_exact_three_records() -> None:
    records = _parse_hash_output(_good_hash_stdout())
    assert len(records) == 3
    by_path = {r.path: r for r in records}
    assert by_path[_CALL_HISTORY_DB_PATH].sha256_hex == GOOD_HASH_DB
    assert by_path[_CALL_HISTORY_WAL_PATH].sha256_hex == GOOD_HASH_WAL
    assert by_path[_CALL_HISTORY_SHM_PATH].sha256_hex == GOOD_HASH_SHM


@pytest.mark.parametrize(
    "bad_stdout",
    [
        b"",
        b"not even close to a hash record",
        (f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH}\n").encode(),  # missing 2
        (
            f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH}\n"
            f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
            f"{'d' * 64} */extra/unexpected/path\n"
        ).encode(),  # extra record
        (
            f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH}\n"
            f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
        ).encode(),  # duplicate, missing wal
        (
            f"{GOOD_HASH_DB} */etc/passwd\n"
            f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
        ).encode(),  # wrong path
        (
            f"{'z' * 64} *{_CALL_HISTORY_DB_PATH}\n"
            f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
        ).encode(),  # non-hex
        (
            f"{'a' * 63} *{_CALL_HISTORY_DB_PATH}\n"
            f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
        ).encode(),  # wrong length
        (
            f"{GOOD_HASH_DB} *{_CALL_HISTORY_DB_PATH} extra open-ended trailer\n"
            f"{GOOD_HASH_WAL} *{_CALL_HISTORY_WAL_PATH}\n"
            f"{GOOD_HASH_SHM} *{_CALL_HISTORY_SHM_PATH}\n"
        ).encode(),  # open-ended text
    ],
)
def test_hash_parser_rejects_bad_output(bad_stdout: bytes) -> None:
    with pytest.raises(IntegrityCheckError):
        _parse_hash_output(bad_stdout)


# --- Timeout, signal, nonzero exit: content-free -----------------------------


def test_capture_snapshot_timeout_is_content_free() -> None:
    def timeout_runner(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

    with pytest.raises(IntegrityCheckError) as exc_info:
        capture_integrity_snapshot(stat_runner=timeout_runner, hash_runner=timeout_runner)
    message = str(exc_info.value)
    assert "ssh" not in message
    assert "/usr/bin/stat" not in message


def test_capture_snapshot_signal_exit_is_content_free() -> None:
    def killed_runner(argv):
        return _completed(argv, returncode=-9, stdout=b"", stderr=b"secret stderr")

    with pytest.raises(IntegrityCheckError) as exc_info:
        capture_integrity_snapshot(stat_runner=killed_runner, hash_runner=killed_runner)
    message = str(exc_info.value)
    assert "-9" not in message
    assert "secret" not in message


def test_capture_snapshot_nonzero_exit_is_content_free() -> None:
    secret_stderr = b"permission denied for /etc/shadow"

    def failing_runner(argv):
        return _completed(argv, returncode=1, stdout=b"", stderr=secret_stderr)

    with pytest.raises(IntegrityCheckError) as exc_info:
        capture_integrity_snapshot(stat_runner=failing_runner, hash_runner=failing_runner)
    assert "permission denied" not in str(exc_info.value)


def test_capture_snapshot_unexpected_exception_is_content_free() -> None:
    def broken_runner(argv):
        raise OSError("ssh binary not found: /secret/identity/path")

    with pytest.raises(IntegrityCheckError) as exc_info:
        capture_integrity_snapshot(stat_runner=broken_runner, hash_runner=broken_runner)
    assert "/secret/identity/path" not in str(exc_info.value)


# --- capture_integrity_snapshot: combines stat + hash, never runs the scan --


def test_capture_integrity_snapshot_combines_stat_and_hash() -> None:
    stat_runner = lambda argv: _completed(argv, 0, _good_stat_stdout())
    hash_runner = lambda argv: _completed(argv, 0, _good_hash_stdout())

    snapshot = capture_integrity_snapshot(stat_runner=stat_runner, hash_runner=hash_runner)
    assert isinstance(snapshot, IntegritySnapshot)
    assert snapshot.call_history.path == _CALL_HISTORY_DB_PATH
    assert snapshot.call_history.size_bytes == GOOD_SIZE
    assert snapshot.call_history.mtime_epoch == GOOD_MTIME
    assert snapshot.call_history.sha256_hex == GOOD_HASH_DB
    assert snapshot.wal.sha256_hex == GOOD_HASH_WAL
    assert snapshot.shm.sha256_hex == GOOD_HASH_SHM


def test_capture_integrity_snapshot_never_invokes_sqlite3() -> None:
    seen_argvs: list[list[str]] = []

    def recording_stat_runner(argv):
        seen_argvs.append(argv)
        return _completed(argv, 0, _good_stat_stdout())

    def recording_hash_runner(argv):
        seen_argvs.append(argv)
        return _completed(argv, 0, _good_hash_stdout())

    capture_integrity_snapshot(stat_runner=recording_stat_runner, hash_runner=recording_hash_runner)
    for argv in seen_argvs:
        assert "/usr/bin/sqlite3" not in " ".join(argv)


# --- compare_integrity_snapshots: per-file booleans + all-equal -------------


def _snapshot(size=GOOD_SIZE, mtime=GOOD_MTIME, sha=GOOD_HASH_DB) -> IntegritySnapshot:
    def _file(path):
        return FileIntegritySnapshot(path=path, size_bytes=size, mtime_epoch=mtime, sha256_hex=sha)

    return IntegritySnapshot(
        call_history=_file(_CALL_HISTORY_DB_PATH),
        wal=_file(_CALL_HISTORY_WAL_PATH),
        shm=_file(_CALL_HISTORY_SHM_PATH),
    )


def test_compare_identical_snapshots_are_all_equal() -> None:
    before = _snapshot()
    after = _snapshot()
    comparison = compare_integrity_snapshots(before, after)
    assert comparison.call_history.all_equal
    assert comparison.wal.all_equal
    assert comparison.shm.all_equal
    assert comparison.all_equal


def test_compare_reports_each_field_independently() -> None:
    before = _snapshot()
    after = _snapshot(size=GOOD_SIZE + 1)
    comparison = compare_integrity_snapshots(before, after)
    assert comparison.call_history.size_equal is False
    assert comparison.call_history.mtime_equal is True
    assert comparison.call_history.sha256_equal is True
    assert comparison.call_history.all_equal is False
    assert comparison.all_equal is False


def test_compare_hash_mismatch_marks_not_all_equal() -> None:
    before = _snapshot()
    after = _snapshot(sha="f" * 64)
    comparison = compare_integrity_snapshots(before, after)
    assert comparison.call_history.sha256_equal is False
    assert comparison.all_equal is False


def test_compare_mtime_mismatch_marks_not_all_equal() -> None:
    before = _snapshot()
    after = _snapshot(mtime=GOOD_MTIME + 1)
    comparison = compare_integrity_snapshots(before, after)
    assert comparison.call_history.mtime_equal is False
    assert comparison.all_equal is False
