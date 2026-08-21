"""Desk SSH driver — run commands on the remote Mac ("the Desk") over SSH.

Composes SSHEnvironment primitives (_build_ssh_command, _establish_connection,
_detect_remote_home, cleanup) without the file-sync / session-snapshot overhead
that the full terminal tool environment carries.

Auth notes baked into the remote command pattern:
- Bare non-interactive SSH on macOS gets only /usr/bin:/bin — wrap in zsh -l -c.
- Keychain is inaccessible from SSH sessions (Background security session).
  Tokens are read from pre-placed files via $(cat ...) inside the remote shell;
  their contents never appear in local output or logs.
"""

import hashlib
import shlex
import subprocess
import tempfile
from pathlib import Path

from tools.environments.base import BaseEnvironment
from tools.environments.ssh import SSHEnvironment, _ensure_ssh_available

# ---------------------------------------------------------------------------
# Desk connection config — one canonical place; wire DESK_SSH_* env vars here
# if you want per-invocation overrides without touching this file.
# ---------------------------------------------------------------------------
import os as _os

DESK_HOST = _os.getenv("DESK_SSH_HOST", "mac-mini.tailb17ed8.ts.net")
DESK_USER = _os.getenv("DESK_SSH_USER", "clayduncan")
DESK_PORT = int(_os.getenv("DESK_SSH_PORT", "22"))
DESK_KEY  = _os.getenv("DESK_SSH_KEY",  "~/.ssh/desk_deploy")

_DESK_CLAUDE_TOKEN_FILE = "~/.hermes-automation/claude_token"
_DESK_GH_TOKEN_FILE     = "~/.hermes-automation/gh_token"


class _DeskSSH(SSHEnvironment):
    """Minimal SSH connection to the Desk — ControlMaster reuse, no file sync.

    Overrides __init__ to skip FileSyncManager and init_session (not needed
    for one-shot command execution). All connection/command-building
    infrastructure is inherited unchanged from SSHEnvironment.
    """

    def __init__(self, host: str, user: str, port: int = 22, key_path: str = ""):
        # Call grandparent directly; SSHEnvironment.__init__ sets up file sync
        # and session snapshot capture which we don't need here.
        BaseEnvironment.__init__(self, cwd="~", timeout=300)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        # Inherited cleanup() checks `if self._sync_manager:` before calling
        # sync_back(), so setting this to None is all we need to skip it.
        self._sync_manager = None

        # Same socket path algorithm as SSHEnvironment.__init__ — keeps
        # ControlMaster socket shareable with any full SSHEnvironment pointed
        # at the same (user, host, port) triple.
        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()
        ).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"

        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()

    def _before_execute(self) -> None:
        pass  # no file sync


def _build_desk_remote_cmd(command: str) -> str:
    """Wrap *command* in the Desk's required env-var + login-shell pattern.

    The $(cat ...) expressions are evaluated by the remote shell — the token
    file contents never appear in local output, logs, or return values.
    """
    return (
        f"CLAUDE_CODE_OAUTH_TOKEN=$(cat {_DESK_CLAUDE_TOKEN_FILE})"
        f" GH_TOKEN=$(cat {_DESK_GH_TOKEN_FILE})"
        f" zsh -l -c {shlex.quote(command)}"
    )


class DeskRunner:
    """Run shell commands and Claude Code non-interactively on the Desk."""

    def __init__(self):
        key = str(Path(DESK_KEY).expanduser())
        self._ssh = _DeskSSH(
            host=DESK_HOST,
            user=DESK_USER,
            port=DESK_PORT,
            key_path=key,
        )

    def run_on_desk(self, command: str, timeout: int = 120) -> str:
        """Run an arbitrary shell command on the Desk and return stdout.

        Raises RuntimeError on non-zero exit. The command is wrapped in
        the login-shell + token-env pattern automatically.
        """
        remote_cmd = _build_desk_remote_cmd(command)
        ssh_cmd = self._ssh._build_ssh_command()
        ssh_cmd.append(remote_cmd)

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Desk command timed out after {timeout}s: {command!r}"
            ) from exc

        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip())[:500]
            raise RuntimeError(
                f"Desk command failed (rc={result.returncode}): {detail}"
            )
        return result.stdout

    def run_claude_on_desk(
        self,
        prompt: str,
        cwd: str = "~",
        timeout: int = 300,
    ) -> str:
        """Run `claude -p '<prompt>' --dangerously-skip-permissions` on the Desk.

        Creates *cwd* with `mkdir -p` if needed, changes into it, then invokes
        claude non-interactively. Returns Claude Code's stdout.

        The cwd is embedded unquoted so zsh can expand `~` when it evaluates
        the `-c` script (shlex.quote wraps in single quotes which suppress `~`
        expansion). Callers must ensure the path contains no shell-unsafe chars.
        """
        if cwd and cwd != "~":
            # Unquoted: zsh evaluates `~` in the -c script body; shlex.quote
            # would wrap it in single quotes and suppress tilde expansion.
            cd_prefix = f"mkdir -p {cwd} && cd {cwd} && "
        else:
            cd_prefix = ""
        inner = (
            f"{cd_prefix}claude -p {shlex.quote(prompt)}"
            f" --dangerously-skip-permissions"
        )
        return self.run_on_desk(inner, timeout=timeout)

    def close(self) -> None:
        self._ssh.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
