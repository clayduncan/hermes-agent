#!/usr/bin/env python3
"""Aggregate the local, content-free ``latency.jsonl`` log into a report.

Reads ``<HERMES_HOME>/logs/latency.jsonl`` (+ rotated backups) written by
``agent/latency_metrics.py`` and prints count/p50/p95/min/max for TTFT and
first-PCM latency, grouped by surface and bounded turn-index band, plus the
turns-1-10-vs-15-25 comparison called for by the Latency L0 acceptance
criteria. Purely local — no network calls, no third-party telemetry.

Usage:
    python scripts/latency_report.py [--hermes-home PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.latency_metrics import aggregate_latency, format_report, read_latency_records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=None,
        help="Override HERMES_HOME (defaults to the profile-resolved home).",
    )
    args = parser.parse_args()

    from hermes_constants import get_hermes_home

    home = args.hermes_home or get_hermes_home()
    log_dir = home / "logs"
    records = list(read_latency_records(log_dir))
    if not records:
        print(f"No latency records found under {log_dir}/latency.jsonl")
        return 0
    print(format_report(aggregate_latency(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
