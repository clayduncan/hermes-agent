"""Deterministic tests for the OPS-114 durable process lock.

Every test points at a temp-dir lock file -- never the real ~/.hermes.
Crash-release is proven with a real forked child process that terminates
while holding the lock without ever calling release() (never with an
artificial PID written into metadata and never with an age/staleness
heuristic): the kernel, not this module's own code, is what must release
the lock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.process_lock import TeamDuncanLock


@pytest.fixture()
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "cron" / "locks" / "team-duncan-automation.lock"


def test_first_acquire_succeeds(lock_dir: Path) -> None:
    lock = TeamDuncanLock(lock_dir=lock_dir)
    result = lock.acquire(mode="desk")
    assert result.acquired is True
    assert result.owner_pid == os.getpid()
    assert lock_dir.is_file()
    lock.release()


def test_real_overlap_second_acquire_is_clean_skip(lock_dir: Path) -> None:
    """A live owner (this test process itself, holding a real kernel flock
    on its own open file descriptor) blocks a second acquirer with a clean
    structured skip -- no exception, no partial state."""
    first = TeamDuncanLock(lock_dir=lock_dir)
    first_result = first.acquire(mode="desk")
    assert first_result.acquired is True

    second = TeamDuncanLock(lock_dir=lock_dir)
    second_result = second.acquire(mode="plaud-reconcile")
    assert second_result.acquired is False
    assert second_result.owner_pid == os.getpid()

    first.release()


def test_release_then_reacquire_succeeds(lock_dir: Path) -> None:
    first = TeamDuncanLock(lock_dir=lock_dir)
    assert first.acquire(mode="desk").acquired is True
    first.release()

    second = TeamDuncanLock(lock_dir=lock_dir)
    result = second.acquire(mode="desk")
    assert result.acquired is True
    second.release()


def test_release_without_acquire_is_a_safe_noop(lock_dir: Path) -> None:
    lock = TeamDuncanLock(lock_dir=lock_dir)
    lock.release()  # must not raise
    assert not lock_dir.exists()


def test_context_manager_releases_on_exit(lock_dir: Path) -> None:
    with TeamDuncanLock(lock_dir=lock_dir) as lock:
        result = lock.acquire(mode="desk")
        assert result.acquired is True
    # The kernel flock was released on __exit__ -- a fresh acquire proves it,
    # since flock (unlike the old mkdir-based lock) never deletes the file.
    probe = TeamDuncanLock(lock_dir=lock_dir)
    probe_result = probe.acquire(mode="desk")
    assert probe_result.acquired is True
    probe.release()


def test_default_lock_dir_is_rooted_under_hermes_home_not_tmp(tmp_path: Path, monkeypatch) -> None:
    """The lock lives under <hermes_home>/cron/locks/, never /tmp."""
    fake_home = tmp_path / "fake-hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(fake_home))

    lock = TeamDuncanLock()
    assert str(lock.lock_dir).startswith(str(fake_home))
    assert "cron" in lock.lock_dir.parts
    assert "locks" in lock.lock_dir.parts
    assert "/tmp/" not in str(lock.lock_dir) + "/"


def test_leftover_metadata_with_no_kernel_lock_does_not_block_acquisition(lock_dir: Path) -> None:
    """A lock file that exists on disk with metadata naming a long-dead
    owner -- but that nobody currently holds an flock on -- must not block
    acquisition. No PID or age check ever runs; the OS-level lock is simply
    free, exactly as if a crashed process's death had already released it."""
    lock_dir.parent.mkdir(parents=True)
    lock_dir.write_text(json.dumps({"pid": 999999999, "mode": "desk", "acquired_at": 0.0}))

    lock = TeamDuncanLock(lock_dir=lock_dir)
    result = lock.acquire(mode="plaud-reconcile")
    assert result.acquired is True
    assert result.stale_recovered is True

    new_meta = json.loads(lock_dir.read_text())
    assert new_meta["pid"] == os.getpid()
    assert new_meta["mode"] == "plaud-reconcile"
    lock.release()


def test_empty_leftover_file_does_not_block_acquisition(lock_dir: Path) -> None:
    """Simulates the exact crash window a two-step mkdir-then-write-metadata
    lock is vulnerable to: a lock artifact exists on disk but the metadata
    write never happened (the owner died first). Acquisition must still
    succeed immediately -- there is no owner-identity gate at all."""
    lock_dir.parent.mkdir(parents=True)
    lock_dir.touch()

    lock = TeamDuncanLock(lock_dir=lock_dir)
    result = lock.acquire(mode="desk")
    assert result.acquired is True
    lock.release()


def test_dead_owner_process_lock_is_released_by_the_kernel_not_by_release(lock_dir: Path) -> None:
    """The direct proof OPS-114 requires: a real process that terminates
    while holding the lock, without ever calling release() (no finally, no
    context manager, no cleanup code of any kind runs -- os._exit() skips
    every one of those, the same as a hard crash would), cannot block the
    next acquirer forever. This is provided entirely by the kernel tearing
    down the dead process's file descriptors on exit, not by any
    staleness/PID/age heuristic in this module."""
    pid = os.fork()
    if pid == 0:
        child_lock = TeamDuncanLock(lock_dir=lock_dir)
        result = child_lock.acquire(mode="desk")
        os._exit(0 if result.acquired else 2)

    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "child failed to acquire"

    parent_lock = TeamDuncanLock(lock_dir=lock_dir)
    result = parent_lock.acquire(mode="plaud-reconcile")
    assert result.acquired is True, "a dead owner must never block the lock forever"
    parent_lock.release()
