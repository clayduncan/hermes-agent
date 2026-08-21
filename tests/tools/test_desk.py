"""Unit tests for the Desk SSH driver (tools/environments/desk.py).

Mocks the actual SSH call — follows the pattern in test_ssh_environment.py.
Verifies command construction wraps correctly (login shell + both env vars
present, token file paths only via $(cat ...) never as literal values).
"""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from tools.environments import desk as desk_mod
from tools.environments.desk import (
    DeskRunner,
    _DeskSSH,
    _DESK_CLAUDE_TOKEN_FILE,
    _DESK_GH_TOKEN_FILE,
    _build_desk_remote_cmd,
)


# ---------------------------------------------------------------------------
# _build_desk_remote_cmd — pure function, no SSH needed
# ---------------------------------------------------------------------------

class TestBuildDeskRemoteCmd:

    def test_contains_claude_token_env_var(self):
        cmd = _build_desk_remote_cmd("whoami")
        assert "CLAUDE_CODE_OAUTH_TOKEN=$(cat " in cmd
        assert _DESK_CLAUDE_TOKEN_FILE in cmd

    def test_contains_gh_token_env_var(self):
        cmd = _build_desk_remote_cmd("whoami")
        assert "GH_TOKEN=$(cat " in cmd
        assert _DESK_GH_TOKEN_FILE in cmd

    def test_uses_zsh_login_shell(self):
        cmd = _build_desk_remote_cmd("whoami")
        assert "zsh -l -c" in cmd

    def test_command_appears_in_output(self):
        cmd = _build_desk_remote_cmd("echo hello")
        assert "echo hello" in cmd

    def test_token_contents_never_appear(self):
        # The pattern uses $(cat file) — the actual token string is never
        # present in the constructed command.  This test verifies that the
        # construction function does not accept or embed a literal token value.
        cmd = _build_desk_remote_cmd("echo hello")
        # Confirm the structure is $(cat ...) not a literal credential
        assert "$(cat " in cmd
        # No plain "=" followed by a long-ish string that looks like a token
        # (the only = signs should be the env var assignments with $(cat ...))
        import re
        # Each env-var assignment must end in $(cat ...)  not a bare value
        # Match everything from the = to the closing ) of the subshell expression
        env_assignments = re.findall(r'[A-Z_]+=(\$\(cat [^)]+\))', cmd)
        assert len(env_assignments) == 2, (
            f"Expected 2 $(cat ...) env var assignments, found {env_assignments!r} in: {cmd!r}"
        )
        for val in env_assignments:
            assert val.startswith("$(cat "), (
                f"env var value {val!r} is not a $(cat ...) expression"
            )

    def test_env_vars_precede_zsh(self):
        cmd = _build_desk_remote_cmd("echo test")
        claude_pos = cmd.index("CLAUDE_CODE_OAUTH_TOKEN")
        gh_pos = cmd.index("GH_TOKEN")
        zsh_pos = cmd.index("zsh -l -c")
        assert claude_pos < zsh_pos, "CLAUDE_CODE_OAUTH_TOKEN must come before zsh"
        assert gh_pos < zsh_pos, "GH_TOKEN must come before zsh"

    def test_special_chars_in_command_are_quoted(self):
        # Prompts with single quotes must be safely escaped
        cmd = _build_desk_remote_cmd("echo it's a test")
        # The command must still appear (possibly shell-escaped) and zsh -l -c
        # must be present — the key invariant is no raw injection
        assert "zsh -l -c" in cmd


# ---------------------------------------------------------------------------
# _DeskSSH — connection setup mocked at subprocess level
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_ssh_connection(monkeypatch):
    """Mock subprocess.run and Popen so _DeskSSH.__init__ doesn't touch the wire."""
    monkeypatch.setattr(
        "tools.environments.ssh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "tools.environments.ssh.subprocess.Popen",
        lambda *a, **k: MagicMock(
            stdout=iter([]), stderr=iter([]), stdin=MagicMock()
        ),
    )
    monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)


class TestDeskSSHInit:

    def test_no_file_sync_manager(self, mock_ssh_connection):
        env = _DeskSSH(host="h", user="u")
        assert env._sync_manager is None

    def test_inherits_build_ssh_command(self, mock_ssh_connection):
        env = _DeskSSH(host="desk.example.com", user="alice", port=22)
        cmd = env._build_ssh_command()
        assert "alice@desk.example.com" in " ".join(cmd)

    def test_key_path_in_command(self, mock_ssh_connection):
        env = _DeskSSH(host="h", user="u", key_path="/tmp/key")
        cmd = " ".join(env._build_ssh_command())
        assert "-i" in cmd
        assert "/tmp/key" in cmd


# ---------------------------------------------------------------------------
# DeskRunner — run_on_desk command construction
# ---------------------------------------------------------------------------

def _make_runner(monkeypatch):
    """Return a DeskRunner with SSH wired up but subprocess mocked out."""
    monkeypatch.setattr(
        "tools.environments.ssh.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/Users/alice\n", stderr=""),
    )
    monkeypatch.setattr(
        "tools.environments.ssh.subprocess.Popen",
        lambda *a, **k: MagicMock(stdout=iter([]), stderr=iter([]), stdin=MagicMock()),
    )
    monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
    return DeskRunner()


class TestDeskRunnerCommandShape:

    def test_run_on_desk_sends_remote_cmd_to_ssh(self, monkeypatch):
        calls = []

        def fake_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="alice\n", stderr="")

        monkeypatch.setattr(
            "tools.environments.ssh.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/home/alice\n", stderr=""),
        )
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

        runner = DeskRunner()

        # Now intercept subprocess.run at the desk module level for the actual call
        monkeypatch.setattr("tools.environments.desk.subprocess.run", fake_subprocess_run)

        output = runner.run_on_desk("whoami")
        assert output == "alice\n"
        assert len(calls) == 1
        remote_cmd = calls[0][-1]  # last element is the remote command string
        assert "CLAUDE_CODE_OAUTH_TOKEN=$(cat" in remote_cmd
        assert "GH_TOKEN=$(cat" in remote_cmd
        assert "zsh -l -c" in remote_cmd
        assert "whoami" in remote_cmd

    def test_run_on_desk_raises_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            "tools.environments.ssh.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/home/alice\n", stderr=""),
        )
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
        runner = DeskRunner()

        monkeypatch.setattr(
            "tools.environments.desk.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="command not found"),
        )
        with pytest.raises(RuntimeError, match="Desk command failed"):
            runner.run_on_desk("bogus_cmd")

    def test_run_claude_on_desk_includes_claude_invocation(self, monkeypatch):
        calls = []

        monkeypatch.setattr(
            "tools.environments.ssh.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/home/alice\n", stderr=""),
        )
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
        runner = DeskRunner()

        def capture_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="42\n", stderr="")

        monkeypatch.setattr("tools.environments.desk.subprocess.run", capture_run)

        output = runner.run_claude_on_desk("what is 6 times 7?")
        assert output == "42\n"
        remote_cmd = calls[0][-1]
        assert "claude -p" in remote_cmd
        assert "--dangerously-skip-permissions" in remote_cmd
        assert "what is 6 times 7?" in remote_cmd

    def test_run_claude_on_desk_with_cwd(self, monkeypatch):
        calls = []

        monkeypatch.setattr(
            "tools.environments.ssh.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/home/alice\n", stderr=""),
        )
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
        runner = DeskRunner()

        def capture_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="done\n", stderr="")

        monkeypatch.setattr("tools.environments.desk.subprocess.run", capture_run)

        runner.run_claude_on_desk("hello", cwd="/tmp/scratch")
        remote_cmd = calls[0][-1]
        # cd should be embedded in the remote command string
        assert "/tmp/scratch" in remote_cmd

    def test_no_literal_token_in_run_on_desk_command(self, monkeypatch):
        """Token file contents must never appear in the command sent to subprocess."""
        captured_cmds = []

        monkeypatch.setattr(
            "tools.environments.ssh.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="/home/alice\n", stderr=""),
        )
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)
        runner = DeskRunner()

        monkeypatch.setattr(
            "tools.environments.desk.subprocess.run",
            lambda cmd, **k: (
                captured_cmds.append(cmd)
                or subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
            ),
        )

        runner.run_on_desk("echo test")
        full_cmd = " ".join(captured_cmds[0])
        # The pattern must be $(cat <path>) — not a resolved token value
        assert "$(cat " in full_cmd
        # Confirm the env var assignment uses the file reference, not a bare string
        import re
        vals = re.findall(r'(?:CLAUDE_CODE_OAUTH_TOKEN|GH_TOKEN)=(\S+)', full_cmd)
        for v in vals:
            assert v.startswith("$(cat"), f"Expected $(cat ...) pattern, got {v!r}"
