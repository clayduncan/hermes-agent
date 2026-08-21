"""Tests for scripts/write_audit_query.py, run against real on-disk log files.

The log data here is produced by the real write clients through the real append
path — no hand-crafted JSON — and spans two months so the cross-file query is
genuinely exercised.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fakes.write_audit_http import SpyTransport
from tools.ghl_client import GHL_API_BASE_URL, GoHighLevelWriteClient
from tools.msgraph_write_client import (
    DEFAULT_GRAPH_BASE_URL,
    MicrosoftGraphContactsWriteClient,
)
from tools.sync_json_http import HttpResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
QUERY_SCRIPT = REPO_ROOT / "scripts" / "write_audit_query.py"

JULY = datetime(2026, 7, 15, 9, 0, 0, tzinfo=timezone.utc)
AUGUST = datetime(2026, 8, 21, 22, 15, 3, 412000, tzinfo=timezone.utc)


def run_query(*args: str) -> list[dict]:
    """Invoke the CLI as a subprocess and parse its concatenated JSON objects."""
    result = subprocess.run(
        [sys.executable, str(QUERY_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return parse_objects(result.stdout)


def parse_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        obj, index = decoder.raw_decode(text, index)
        objects.append(obj)
    return objects


@pytest.fixture
def populated_log(tmp_path: Path) -> Path:
    """Two months of real entries written by the real clients."""
    log_dir = tmp_path / "write_audit"

    graph = SpyTransport(DEFAULT_GRAPH_BASE_URL)
    graph.route("POST", "/me/contacts", {"id": "c-1", "displayName": "July Contact"})
    graph.route("GET", "/me/contacts/c-1", {"id": "c-1", "displayName": "July Contact"})
    MicrosoftGraphContactsWriteClient(
        lambda: "tok", request_fn=graph, log_dir=log_dir, clock=lambda: JULY
    ).create_contact({"displayName": "July Contact"}, trigger="July seed")

    graph_august = SpyTransport(DEFAULT_GRAPH_BASE_URL)
    graph_august.route(
        "GET",
        "/me/contacts/c-1",
        {"id": "c-1", "displayName": "July Contact"},
        {"id": "c-1", "displayName": "August Contact"},
    )
    graph_august.route("PATCH", "/me/contacts/c-1", {"id": "c-1", "displayName": "August Contact"})
    MicrosoftGraphContactsWriteClient(
        lambda: "tok", request_fn=graph_august, log_dir=log_dir, clock=lambda: AUGUST
    ).update_contact("c-1", {"displayName": "August Contact"}, trigger="August touch")

    ghl = SpyTransport(GHL_API_BASE_URL)
    ghl.route("GET", "/contacts/g-1", {"contact": {"id": "g-1", "tags": []}})
    ghl.route("DELETE", "/contacts/g-1", HttpResponse(status_code=200, body=b'{"succeded":true}'))
    GoHighLevelWriteClient(
        "tracey",
        api_key="fake",
        location_id="loc-1",
        request_fn=ghl,
        log_dir=log_dir,
        clock=lambda: AUGUST,
    ).delete_contact("g-1", trigger="August GHL cleanout")

    return log_dir


class TestQueryAgainstRealFiles:
    def test_two_month_files_exist(self, populated_log: Path) -> None:
        assert sorted(p.name for p in populated_log.glob("*.jsonl")) == [
            "2026-07.jsonl",
            "2026-08.jsonl",
        ]

    def test_record_id_filter_spans_months_in_chronological_order(
        self, populated_log: Path
    ) -> None:
        found = run_query("--log-dir", str(populated_log), "--record-id", "c-1")

        # July create outcome (record_id assigned post-write) + both August update lines.
        assert [entry["timestamp"] for entry in found] == sorted(
            entry["timestamp"] for entry in found
        )
        assert [entry["audit_phase"] for entry in found] == ["outcome", "intent", "outcome"]
        assert found[0]["timestamp"].startswith("2026-07")
        assert found[1]["timestamp"].startswith("2026-08")
        assert all(entry["record_id"] == "c-1" for entry in found)

    def test_destination_filter(self, populated_log: Path) -> None:
        graph_entries = run_query(
            "--log-dir", str(populated_log), "--destination", "msgraph_contacts"
        )
        assert len(graph_entries) == 4  # create intent+outcome, update intent+outcome
        assert {entry["destination"] for entry in graph_entries} == {"msgraph_contacts"}

        ghl_entries = run_query("--log-dir", str(populated_log), "--destination", "ghl_contacts")
        assert len(ghl_entries) == 2
        assert {entry["destination"] for entry in ghl_entries} == {"ghl_contacts"}
        assert {entry["record_id"] for entry in ghl_entries} == {"g-1"}

    def test_both_filters_combine_as_an_intersection(self, populated_log: Path) -> None:
        found = run_query(
            "--log-dir",
            str(populated_log),
            "--destination",
            "ghl_contacts",
            "--record-id",
            "g-1",
        )
        assert len(found) == 2
        assert {entry["operation"] for entry in found} == {"delete"}

        # An ID that exists, but under a different destination, matches nothing.
        assert (
            run_query(
                "--log-dir",
                str(populated_log),
                "--destination",
                "ghl_contacts",
                "--record-id",
                "c-1",
            )
            == []
        )

    def test_record_id_never_matches_a_null_record_id(self, populated_log: Path) -> None:
        """The July create's intent line has ``record_id: null`` and must stay unmatched."""
        assert run_query("--log-dir", str(populated_log), "--record-id", "None") == []
        assert run_query("--log-dir", str(populated_log), "--record-id", "null") == []

    def test_no_filters_returns_everything_in_chronological_order(
        self, populated_log: Path
    ) -> None:
        found = run_query("--log-dir", str(populated_log))
        assert len(found) == 6
        timestamps = [entry["timestamp"] for entry in found]
        assert timestamps == sorted(timestamps)

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(QUERY_SCRIPT), "--log-dir", str(tmp_path / "nope")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
        assert "No write audit log directory" in result.stderr

    def test_after_fetch_failed_filter_finds_flagged_entries(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "write_audit"
        transport = SpyTransport(DEFAULT_GRAPH_BASE_URL)
        transport.route("POST", "/me/contacts", {"id": "c-7", "displayName": "Unverifiable"})
        transport.route("GET", "/me/contacts/c-7", HttpResponse(status_code=500))
        MicrosoftGraphContactsWriteClient(
            lambda: "tok",
            request_fn=transport,
            log_dir=log_dir,
            max_retries=0,
            clock=lambda: AUGUST,
        ).create_contact({"displayName": "Unverifiable"}, trigger="read-back drill")

        found = run_query("--log-dir", str(log_dir), "--after-fetch-failed")
        assert len(found) == 1
        assert found[0]["after"] == "UNKNOWN — verify manually"
        assert found[0]["record_id"] == "c-7"

    def test_a_corrupt_line_does_not_hide_the_rest_of_the_history(
        self, populated_log: Path
    ) -> None:
        with open(populated_log / "2026-08.jsonl", "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")

        found = run_query("--log-dir", str(populated_log))
        assert len(found) == 6

    def test_output_is_pretty_printed(self, populated_log: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(QUERY_SCRIPT), "--log-dir", str(populated_log), "--record-id", "g-1"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert result.returncode == 0
        assert '\n  "destination": "ghl_contacts",' in result.stdout
