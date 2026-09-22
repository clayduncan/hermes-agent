"""Tests for the OPS-110 production Plaud transport.

Monkeypatches the two private tools.mcp_tool helpers this module calls
(`_get_connected_server_for_call`, `_run_on_mcp_loop`) with an in-memory
fake session -- no live MCP connection, no real credentials, no network.

Fixtures here use the exact real Plaud MCP contract: tool names
`list_files`/`get_transcript`, raw fields `id`/`name`/`created_at`/
`serial_number`/`start_at`/`duration` (milliseconds) for files, and
`start_time`/`end_time`/`content`/`speaker`/`original_speaker` for
transcript segments. There is no `list_recordings`, `get_recording`, or
`get_note` tool, and no `outline`/`transaction_polish`/`mark_memo`
transcript block -- this file proves those can never be reached.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

import tools.mcp_tool as mcp_tool_module
from plugins.team_duncan_contacts.collectors.live_plaud_transport import (
    ALLOWED_PLAUD_TOOLS,
    LIST_FILES_PAGE_SIZE,
    TRANSCRIPT_BLOCK,
    DisallowedPlaudToolError,
    LivePlaudTransport,
    PlaudMcpToolError,
    TOOL_GET_TRANSCRIPT,
    TOOL_LIST_FILES,
)

CORY_RECORDING_ID = "of_43c744fd9e22636f06b4508908b73531"
CORY_START_AT = "2026-09-17T19:22:59+00:00"
CORY_DURATION_MS = 1305000


@dataclass
class _ContentBlock:
    text: str | None = None


@dataclass
class _FakeCallToolResult:
    content: list = field(default_factory=list)
    is_error: bool = False


class _FakeSession:
    """Dispatches by tool name to a list of queued payloads (one per call),
    or a single payload reused for every call of that tool name."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        payload = self._responses[name]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, list):
            payload = payload[len([c for c in self.calls if c[0] == name]) - 1]
        return _FakeCallToolResult(content=[_ContentBlock(text=json.dumps(payload))])


class _FakeServer:
    def __init__(self, session) -> None:
        self.session = session


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, server: Any) -> None:
    monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", lambda name: server)

    def _run_on_loop(coro_or_factory, timeout=30):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool_module, "_run_on_mcp_loop", _run_on_loop)


def _file_entry(file_id, start_at, duration_ms, *, name="Call recording") -> dict:
    return {
        "id": file_id,
        "name": name,
        "created_at": start_at,
        "serial_number": "SN-1",
        "start_at": start_at,
        "duration": duration_ms,
    }


class TestStructuralAllowlist:
    def test_disallowed_tool_never_reaches_connected_server_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail_if_called(name):
            raise AssertionError("must not look up a connected server for a disallowed tool")

        monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", _fail_if_called)
        transport = LivePlaudTransport()
        for bad_tool in ("list_recordings", "get_recording", "get_note", "get_summary",
                          "get_mind_map", "ask_plaud"):
            with pytest.raises(DisallowedPlaudToolError):
                transport._call(bad_tool, {})

    def test_allowlist_is_exactly_list_files_and_get_transcript(self) -> None:
        assert ALLOWED_PLAUD_TOOLS == {TOOL_LIST_FILES, TOOL_GET_TRANSCRIPT}
        assert "list_recordings" not in ALLOWED_PLAUD_TOOLS
        assert "get_recording" not in ALLOWED_PLAUD_TOOLS
        assert "get_note" not in ALLOWED_PLAUD_TOOLS


class TestListFilesArguments:
    def test_list_files_calls_use_fixed_page_size_at_least_100(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({TOOL_LIST_FILES: {"type": "list", "data": [], "page": 1, "page_size": LIST_FILES_PAGE_SIZE}})
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        transport.get_deployment_boundary()
        assert LIST_FILES_PAGE_SIZE >= 100
        name, args = session.calls[0]
        assert name == TOOL_LIST_FILES
        assert args["page"] == 1
        assert args["page_size"] == LIST_FILES_PAGE_SIZE
        assert "query" not in args


class TestDeploymentBoundary:
    def test_boundary_is_newest_start_at_across_a_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [
                        _file_entry("f-1", "2026-01-01T00:00:00+00:00", 60_000),
                        _file_entry("f-2", "2026-03-01T00:00:00+00:00", 60_000),
                        _file_entry("f-3", "2026-02-01T00:00:00+00:00", 60_000),
                    ],
                    "page": 1,
                    "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        boundary = transport.get_deployment_boundary()
        assert boundary == "2026-03-01T00:00:00+00:00"

    def test_boundary_falls_back_to_now_when_no_records_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {TOOL_LIST_FILES: {"type": "list", "data": [], "page": 1, "page_size": LIST_FILES_PAGE_SIZE}}
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        boundary = transport.get_deployment_boundary()
        assert boundary  # a real ISO timestamp, not an empty/None value


class TestFetchRecordsSince:
    def test_maps_id_start_at_and_millisecond_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [_file_entry(CORY_RECORDING_ID, CORY_START_AT, CORY_DURATION_MS)],
                    "page": 1,
                    "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        records = transport.fetch_records_since("2026-09-17T00:00:00+00:00")
        assert len(records) == 1
        rec = records[0]
        assert rec["recording_id"] == CORY_RECORDING_ID
        assert rec["start_time"] == CORY_START_AT
        assert rec["duration_s"] == 1305
        assert "duration_ms" not in rec
        assert rec["transcript_available"] is None
        assert rec["summary_available"] is None

    def test_excludes_records_at_or_before_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [
                        _file_entry("old", "2026-01-01T00:00:00+00:00", 1000),
                        _file_entry("at-checkpoint", "2026-06-01T00:00:00+00:00", 1000),
                        _file_entry("new", "2026-07-01T00:00:00+00:00", 1000),
                    ],
                    "page": 1,
                    "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        records = transport.fetch_records_since("2026-06-01T00:00:00+00:00")
        assert [r["recording_id"] for r in records] == ["new"]

    def test_no_checkpoint_means_no_backfill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail_if_called(name):
            raise AssertionError("must not call list_files with no checkpoint")

        monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", _fail_if_called)
        transport = LivePlaudTransport()
        assert transport.fetch_records_since(None) == []

    def test_stops_paginating_once_a_page_has_no_newer_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page_1 = {
            "type": "list",
            "data": [_file_entry(f"p1-{i}", "2026-08-01T00:00:00+00:00", 1000)
                     for i in range(LIST_FILES_PAGE_SIZE)],
            "page": 1, "page_size": LIST_FILES_PAGE_SIZE,
        }
        page_2 = {
            "type": "list",
            "data": [_file_entry("older", "2026-01-01T00:00:00+00:00", 1000)],
            "page": 2, "page_size": LIST_FILES_PAGE_SIZE,
        }
        session = _FakeSession({TOOL_LIST_FILES: [page_1, page_2]})
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        records = transport.fetch_records_since("2026-06-01T00:00:00+00:00")
        assert len(records) == LIST_FILES_PAGE_SIZE
        assert len(session.calls) == 2, "must fetch the boundary page once to confirm no more newer records"


class TestFetchRecordByIdentity:
    def test_finds_exact_id_match_without_a_query_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [_file_entry(CORY_RECORDING_ID, CORY_START_AT, CORY_DURATION_MS)],
                    "page": 1, "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        record = transport.fetch_record_by_identity(CORY_RECORDING_ID)
        assert record is not None
        assert record["recording_id"] == CORY_RECORDING_ID
        assert record["duration_s"] == 1305
        for _, args in session.calls:
            assert "query" not in args

    def test_raises_when_id_not_found_within_bounded_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {TOOL_LIST_FILES: {"type": "list", "data": [], "page": 1, "page_size": LIST_FILES_PAGE_SIZE}}
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.fetch_record_by_identity("does-not-exist")

    def test_never_infers_identity_from_name_or_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record whose *name* matches the requested id, but whose *id*
        does not, must never be returned -- identity is the raw `id` field
        only."""
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [_file_entry("different-id", "2026-01-01T00:00:00+00:00", 1000,
                                          name="does-not-exist")],
                    "page": 1, "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.fetch_record_by_identity("does-not-exist")


class TestTranscriptMethod:
    def test_fetch_transcript_page_requests_transaction_block_and_parses_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_GET_TRANSCRIPT: {
                    "file_id": CORY_RECORDING_ID,
                    "block": "transaction",
                    "total": 1,
                    "offset": 0,
                    "limit": 500,
                    "returned": 1,
                    "next_cursor": None,
                    "segments": [
                        {
                            "start_time": 0.0,
                            "end_time": 1.2,
                            "content": "hello",
                            "speaker": "Cory",
                            "original_speaker": "Speaker 1",
                        }
                    ],
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        page = transport.fetch_transcript_page(CORY_RECORDING_ID, None)
        assert page.segments[0].speaker == "Cory"
        assert page.segments[0].text == "hello"
        assert page.segments[0].start_time == 0.0
        assert page.segments[0].end_time == 1.2
        assert page.next_cursor is None

        called_name, called_args = session.calls[0]
        assert called_name == TOOL_GET_TRANSCRIPT
        assert called_args["file_id"] == CORY_RECORDING_ID
        assert called_args["block"] == "transaction"
        assert called_args["block"] == TRANSCRIPT_BLOCK

    def test_public_method_has_no_parameter_to_select_a_different_block(self) -> None:
        import inspect

        params = inspect.signature(LivePlaudTransport.fetch_transcript_page).parameters
        assert "block" not in params

    def test_cursor_is_forwarded_when_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession(
            {TOOL_GET_TRANSCRIPT: {"segments": [], "next_cursor": None}}
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        transport.fetch_transcript_page(CORY_RECORDING_ID, "cursor-123")
        _, args = session.calls[0]
        assert args["cursor"] == "cursor-123"


class TestErrorHandling:
    def test_not_connected_raises_plaud_mcp_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", lambda name: None)
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.get_deployment_boundary()

    def test_mcp_error_result_raises_plaud_mcp_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession({TOOL_LIST_FILES: {}})

        async def _erroring_call_tool(name, arguments):
            return _FakeCallToolResult(content=[_ContentBlock(text="boom")], is_error=True)

        session.call_tool = _erroring_call_tool
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.get_deployment_boundary()


class TestCoryRecordingFixture:
    """The ticket's verified real fixture: recording
    of_43c744fd9e22636f06b4508908b73531, start 2026-09-17T19:22:59,
    duration 1,305,000 ms."""

    def test_cory_fixture_maps_exactly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_FILES: {
                    "type": "list",
                    "data": [_file_entry(CORY_RECORDING_ID, CORY_START_AT, CORY_DURATION_MS,
                                          name="Cory Vasquez call")],
                    "page": 1, "page_size": LIST_FILES_PAGE_SIZE,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        record = transport.fetch_record_by_identity(CORY_RECORDING_ID)
        assert record == {
            "recording_id": CORY_RECORDING_ID,
            "start_time": CORY_START_AT,
            "duration_s": 1305,
            "transcript_available": None,
            "summary_available": None,
        }
