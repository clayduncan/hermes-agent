#!/usr/bin/env python3
"""Manual test / one-shot runner for the Desk SSH driver.

Usage:
    python tools/desk_run.py "whoami && pwd"
    python tools/desk_run.py --claude "list ~/Desktop and tell me what's there"
    python tools/desk_run.py --claude --cwd ~/hermes-automation/scratch-test "write a haiku"
    python tools/desk_run.py --timeout 60 "brew list | head -5"

DESK_SSH_{HOST,USER,PORT,KEY} env vars override the compiled-in defaults.
"""

import argparse
import sys

# Ensure project root is on path when run directly
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.environments.desk import DeskRunner


def main():
    parser = argparse.ArgumentParser(
        description="Run a command or Claude Code prompt on the Desk over SSH",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command_or_prompt",
        help="Shell command (default) or Claude prompt (with --claude)",
    )
    parser.add_argument(
        "--claude", "-c",
        action="store_true",
        help="Run as `claude -p '<prompt>' --dangerously-skip-permissions`",
    )
    parser.add_argument(
        "--cwd",
        default="~",
        help="Working directory on the Desk (only used with --claude; default: ~)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300)",
    )
    args = parser.parse_args()

    with DeskRunner() as desk:
        if args.claude:
            output = desk.run_claude_on_desk(
                args.command_or_prompt,
                cwd=args.cwd,
                timeout=args.timeout,
            )
        else:
            output = desk.run_on_desk(args.command_or_prompt, timeout=args.timeout)

    print(output, end="" if output.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
