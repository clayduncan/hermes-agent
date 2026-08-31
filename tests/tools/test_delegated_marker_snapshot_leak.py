"""Regression tests for OPS-63: HERMES_DELEGATED_CHILD_CONTEXT snapshot leak.

Root cause: LocalEnvironment is shared across normal-parent and delegated-child
task IDs in default shared-container mode. When a delegated-child command runs,
its subprocess env carries HERMES_DELEGATED_CHILD_CONTEXT=1 (injected by
_scrub_delegated_child_kanban_env via scrub_kanban_env). At command end,
_wrap_command dumps the current bash env to the shared snapshot via export -p.
Without the fix, HERMES_DELEGATED_CHILD_CONTEXT was captured in that dump and
persisted into the snapshot, sourced by the next normal parent command, making
the parent appear to run inside a delegated child and receive incorrect
Kanban-mutation denials.

Live proof (pre-fix): the active shared snapshot contained
``declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"`` at line 32 while a normal
desktop session had no marker in its process environment.

Fix: _export_dump_excluding_session_vars in tools/environments/base.py now unsets
HERMES_DELEGATED_CHILD_CONTEXT before export -p so it is never written to the
snapshot, while the per-command injection via _make_run_env continues to provide
the marker to genuine delegated-child subprocesses.
"""

from __future__ import annotations

import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Unit: snapshot dump snippet must unset HERMES_DELEGATED_CHILD_CONTEXT
# ---------------------------------------------------------------------------

def test_export_snippet_excludes_delegated_child_marker():
    """_export_dump_excluding_session_vars must unset HERMES_DELEGATED_CHILD_CONTEXT.

    This is the regression sentinel: fails against the pre-fix implementation
    where HERMES_DELEGATED_CHILD_CONTEXT was absent from the unset list.
    """
    from tools.environments.base import _export_dump_excluding_session_vars

    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')

    assert "HERMES_DELEGATED_CHILD_CONTEXT" in snippet, (
        "HERMES_DELEGATED_CHILD_CONTEXT must be in the unset list so it is "
        "never serialized into the shared snapshot (OPS-63)"
    )
    # Must appear in the unset block, before export -p.
    unset_block = snippet.split("export -p")[0]
    assert "HERMES_DELEGATED_CHILD_CONTEXT" in unset_block, (
        "HERMES_DELEGATED_CHILD_CONTEXT must be unset BEFORE export -p runs"
    )


# ---------------------------------------------------------------------------
# Integration: real bash validates unset-before-export for the marker
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_snapshot_rewrite_does_not_persist_delegated_child_marker(tmp_path):
    """With HERMES_DELEGATED_CHILD_CONTEXT=1 in the shell env, a snapshot dump
    must not capture it: unset-before-export must fire first.

    Drives a real bash process to validate the multiline-safe
    unset-before-export mechanism (issue #71296 path) for this marker.
    """
    import shlex
    import subprocess

    from tools.environments.base import _export_dump_excluding_session_vars

    snap = tmp_path / "snap.sh"
    dump_snippet = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    marker = "HERMES_DELEGATED_CHILD_CONTEXT"

    script = f"""
set -e
export {marker}
{dump_snippet}
"""
    env = os.environ.copy()
    env[marker] = "1"

    bash = "/bin/bash" if os.path.exists("/bin/bash") else "bash"
    proc = subprocess.run(
        [bash, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"bash dump script failed rc={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert snap.exists(), "snapshot file was not written"
    snap_content = snap.read_text()
    assert marker not in snap_content, (
        f"HERMES_DELEGATED_CHILD_CONTEXT leaked into the snapshot.\n"
        f"This is the OPS-63 pre-fix failure.\n"
        f"Snapshot:\n{snap_content}"
    )


# ---------------------------------------------------------------------------
# Full OPS-63 sequence with a real LocalEnvironment
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_delegated_marker_not_leaked_to_parent_via_snapshot(tmp_path):
    """Reproduce the full OPS-63 leak sequence with a real LocalEnvironment.

    Sequence:
    1. Shared LocalEnvironment starts clean: snapshot has no marker.
    2. Delegated-child context runs a command and sees marker=1.
    3. Snapshot rewrite occurs after the delegated-child command.
    4. Normal parent command does NOT see the marker (pre-fix bug: it did).
    5. Another delegated-child command still sees marker=1.
    """
    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        # Step 1: snapshot starts clean.
        if os.path.exists(env._snapshot_path):
            snap_text = open(env._snapshot_path).read()
            assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snap_text, (
                "Snapshot already had the marker before any delegated run"
            )

        # Step 2: delegated-child command sees marker=1.
        with delegated_child_context():
            child_result = env.execute(
                "printf '%s' \"$HERMES_DELEGATED_CHILD_CONTEXT\"",
                timeout=15,
            )
        assert child_result["output"].strip() == "1", (
            f"Delegated child did not see marker=1: {child_result['output']!r}"
        )

        # Step 3: snapshot must not contain the marker after child's rewrite.
        if os.path.exists(env._snapshot_path):
            snap_text = open(env._snapshot_path).read()
            assert "HERMES_DELEGATED_CHILD_CONTEXT" not in snap_text, (
                "Marker leaked into snapshot after delegated-child command "
                "(pre-fix: was at line 32 of the live shared snapshot).\n"
                f"Snap: {snap_text[:500]}"
            )

        # Step 4: normal parent command must NOT see the marker.
        parent_result = env.execute(
            "printf '%s' \"${HERMES_DELEGATED_CHILD_CONTEXT:-absent}\"",
            timeout=15,
        )
        assert parent_result["output"].strip() == "absent", (
            f"Parent command saw leaked marker: {parent_result['output']!r}\n"
            "This is the OPS-63 bug: marker persisted via shared snapshot."
        )

        # Step 5: another delegated-child command still sees marker=1.
        with delegated_child_context():
            child_result2 = env.execute(
                "printf '%s' \"$HERMES_DELEGATED_CHILD_CONTEXT\"",
                timeout=15,
            )
        assert child_result2["output"].strip() == "1", (
            f"Second delegated-child command did not see marker=1: "
            f"{child_result2['output']!r}"
        )
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# Kanban isolation remains intact after the fix
# ---------------------------------------------------------------------------

def _setup_running_kanban_task(monkeypatch, tmp_path):
    """Create a minimal running Kanban task for isolation tests.

    Returns (kb, task_id, workspace_path).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "parent-workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "parent-worker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="parent",
            assignee="parent-worker",
            workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        run_id = claim.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return kb, tid, workspace


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX bash and subprocess CLI")
def test_delegated_child_kanban_cli_mutation_denied_post_fix(monkeypatch, tmp_path):
    """Delegated-child Kanban CLI mutation stays denied after the OPS-63 fix.

    Verifies that fixing snapshot persistence does not weaken the Kanban
    mutation guard: a child context shelling out to the CLI must still be
    denied board deletion.
    """
    import shlex
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[2]

    kb, _tid, _workspace = _setup_running_kanban_task(monkeypatch, tmp_path)
    kb.create_board("victim")
    assert kb.board_exists("victim")

    from agent.delegation_context import delegated_child_context
    from tools.environments.local import LocalEnvironment

    code = (
        "from hermes_cli import kanban; "
        "import argparse; "
        "p=argparse.ArgumentParser(); "
        "sub=p.add_subparsers(dest='cmd'); "
        "kanban.build_parser(sub); "
        "args=p.parse_args(['kanban','boards','rm','victim','--delete']); "
        "raise SystemExit(kanban.kanban_command(args))"
    )
    cmd = (
        f"PYTHONPATH={shlex.quote(str(_REPO_ROOT))} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    )
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        with delegated_child_context():
            result = env.execute(cmd, timeout=15)
    finally:
        env.cleanup()

    assert result["returncode"] == 1, (
        "CLI must exit non-zero when delegated-child Kanban mutation is denied"
    )
    assert "delegate_task child contexts cannot mutate Kanban tasks" in result["output"]
    assert kb.board_exists("victim"), "board must not be deleted"


def test_delegated_child_direct_kanban_db_mutation_denied(monkeypatch, tmp_path):
    """Direct kanban_db mutation from a delegated-child context is denied.

    The DB-layer guard (_assert_not_delegated_child_mutation) must remain
    intact after the OPS-63 fix. Tests the in-process ContextVar path used by
    real delegated children.
    """
    kb, tid, _workspace = _setup_running_kanban_task(monkeypatch, tmp_path)

    from agent.delegation_context import delegated_child_context

    with delegated_child_context():
        with pytest.raises(PermissionError, match="delegate_task child contexts cannot mutate"):
            conn = kb.connect()
            try:
                kb.complete_task(conn, tid, summary="child tried to complete parent")
            finally:
                conn.close()

    # Task must remain running: the guard fired before any mutation.
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
    finally:
        conn.close()
    assert task.status == "running", (
        f"Task must stay running after denied mutation, got {task.status!r}"
    )
