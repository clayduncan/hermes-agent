#!/usr/bin/env python3
"""Query the append-only write audit log.

Reads every ``~/.hermes/logs/write_audit/YYYY-MM.jsonl`` file, filters, and
prints matching entries as pretty JSON in chronological order.  Read-only: this
script never opens a log file for writing.

Examples:
  scripts/write_audit_query.py --record-id AAMkAD...
  scripts/write_audit_query.py --destination msgraph_contacts
  scripts/write_audit_query.py --destination ghl_contacts --record-id abc123
  scripts/write_audit_query.py --after-fetch-failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# Allow running as a standalone script from anywhere in the repo.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.write_audit_log import (  # noqa: E402
    KNOWN_DESTINATIONS,
    default_write_audit_dir,
    iter_entries,
)


def collect(
    log_dir: Path,
    *,
    record_id: str | None = None,
    destination: str | None = None,
    after_fetch_failed: bool = False,
) -> list[dict[str, Any]]:
    """Return matching entries, oldest first.

    Filters combine with AND.  Ordering is by the entry's own ``timestamp`` (ISO8601
    UTC, so lexicographic order is chronological), with file name and line number as
    tie-breakers so two entries written in the same millisecond keep their on-disk
    order.
    """
    matches: list[tuple[str, str, int, dict[str, Any]]] = []
    for path, line_number, entry in iter_entries(log_dir):
        if record_id is not None:
            entry_id = entry.get("record_id")
            # A null record_id (a create's intent line) matches no --record-id value,
            # including the literal string "None".
            if entry_id is None or str(entry_id) != str(record_id):
                continue
        if destination is not None and entry.get("destination") != destination:
            continue
        if after_fetch_failed and entry.get("after_fetch_failed") is not True:
            continue
        matches.append((str(entry.get("timestamp") or ""), path.name, line_number, entry))

    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    return [entry for _, _, _, entry in matches]


def render(entries: Iterable[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(entry, indent=2, ensure_ascii=False) for entry in entries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--record-id",
        metavar="ID",
        help="Only entries whose record_id matches, across all months",
    )
    parser.add_argument(
        "--destination",
        metavar="NAME",
        help=f"Only entries for this destination ({', '.join(KNOWN_DESTINATIONS)})",
    )
    parser.add_argument(
        "--after-fetch-failed",
        action="store_true",
        help="Only entries whose post-write read-back failed (after_fetch_failed: true)",
    )
    parser.add_argument(
        "--log-dir",
        metavar="PATH",
        help="Override the log directory (default: ~/.hermes/logs/write_audit)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = Path(args.log_dir) if args.log_dir else default_write_audit_dir()

    if not log_dir.is_dir():
        print(f"No write audit log directory at {log_dir}", file=sys.stderr)
        return 0

    entries = collect(
        log_dir,
        record_id=args.record_id,
        destination=args.destination,
        after_fetch_failed=args.after_fetch_failed,
    )
    if entries:
        print(render(entries))
    else:
        print("No matching write audit entries.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
