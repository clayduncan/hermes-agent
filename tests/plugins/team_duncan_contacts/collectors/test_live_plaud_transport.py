"""Tests for the OPS-110 production Plaud transport.

Monkeypatches the two private tools.mcp_tool helpers this module calls
(`_get_connected_server_for_call`, `_run_on_mcp_loop`) with an in-memory
fake session -- no live MCP connection, no real credentials, no network.
Proves the structural tool-name allowlist: a disallowed tool name is
rejected before the connected-server lookup is ever reached.
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
    DisallowedPlaudToolError,
    LivePlaudTransport,
    PlaudMcpToolError,
    TOOL_GET_RECORDING,
    TOOL_GET_TRANSCRIPT,
    TOOL_LIST_RECORDINGS,
)


@dataclass
class _ContentBlock:
    text: str | None = None


@dataclass
class _FakeCallToolResult:
    content: list = field(default_factory=list)
    is_error: bool = False


class _FakeSession:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        payload = self._responses[name]
        if isinstance(payload, Exception):
            raise payload
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


class TestStructuralAllowlist:
    def test_disallowed_tool_never_reaches_connected_server_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail_if_called(name):
            raise AssertionError("must not look up a connected server for a disallowed tool")

        monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", _fail_if_called)
        transport = LivePlaudTransport()
        with pytest.raises(DisallowedPlaudToolError):
            transport._call("get_note", {})
        with pytest.raises(DisallowedPlaudToolError):
            transport._call("get_summary", {})
        with pytest.raises(DisallowedPlaudToolError):
            transport._call("get_mind_map", {})
        with pytest.raises(DisallowedPlaudToolError):
            transport._call("ask_plaud", {})

    def test_allowlist_is_exactly_the_three_metadata_and_transcript_tools(self) -> None:
        assert ALLOWED_PLAUD_TOOLS == {TOOL_LIST_RECORDINGS, TOOL_GET_RECORDING, TOOL_GET_TRANSCRIPT}


class TestMetadataMethods:
    def test_get_deployment_boundary_calls_list_recordings_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({TOOL_LIST_RECORDINGS: {"boundary": "rec-100"}})
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        boundary = transport.get_deployment_boundary()
        assert boundary == "rec-100"
        assert [c[0] for c in session.calls] == [TOOL_LIST_RECORDINGS]

    def test_fetch_records_since_normalizes_millisecond_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_LIST_RECORDINGS: {
                    "records": [
                        {
                            "recording_id": "of_43c744fd9e22636f06b4508908b73531",
                            "start_time": "2026-09-17T19:22:59+00:00",
                            "duration_ms": 1305000,
                            "caller_handle": "+15551234567",
                            "transcript_available": True,
                            "summary_available": False,
                        }
                    ]
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        records = transport.fetch_records_since(None)
        assert len(records) == 1
        assert records[0]["duration_s"] == 1305
        assert "duration_ms" not in records[0]

    def test_fetch_record_by_identity_returns_none_for_empty_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession({TOOL_GET_RECORDING: {}})
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        assert transport.fetch_record_by_identity("missing") is None

    def test_fetch_record_by_identity_unwraps_record_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_GET_RECORDING: {
                    "record": {
                        "recording_id": "rec-1", "start_time": "2026-01-01T00:00:00+00:00",
                        "duration_s": 30, "caller_handle": "+15551234567",
                    }
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        record = transport.fetch_record_by_identity("rec-1")
        assert record["recording_id"] == "rec-1"
        assert record["duration_s"] == 30


class TestTranscriptMethod:
    def test_fetch_transcript_page_requests_transaction_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _FakeSession(
            {
                TOOL_GET_TRANSCRIPT: {
                    "segments": [{"speaker": "Cory", "text": "hello"}],
                    "next_cursor": None,
                }
            }
        )
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        page = transport.fetch_transcript_page("rec-1", None)
        assert page.segments[0].speaker == "Cory"
        assert page.segments[0].text == "hello"
        assert page.next_cursor is None
        called_name, called_args = session.calls[0]
        assert called_name == TOOL_GET_TRANSCRIPT
        assert called_args["type"] == "transaction"


class TestErrorHandling:
    def test_not_connected_raises_plaud_mcp_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_tool_module, "_get_connected_server_for_call", lambda name: None)
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.get_deployment_boundary()

    def test_mcp_error_result_raises_plaud_mcp_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _FakeSession({TOOL_LIST_RECORDINGS: {}})
        session._responses[TOOL_LIST_RECORDINGS] = {}

        async def _erroring_call_tool(name, arguments):
            return _FakeCallToolResult(content=[_ContentBlock(text="boom")], is_error=True)

        session.call_tool = _erroring_call_tool
        _patch_mcp(monkeypatch, _FakeServer(session))
        transport = LivePlaudTransport()
        with pytest.raises(PlaudMcpToolError):
            transport.get_deployment_boundary()
