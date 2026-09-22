"""OPS-110 production Plaud transport: Hermes' existing native MCP client.

This is the one place this build talks to a live Plaud connection. It does
not open a second credential store, scrape the Plaud UI, or call Plaud's
undocumented API directly -- it reuses the same connected-server registry
and background MCP event loop every other MCP tool call in this repo goes
through (`tools.mcp_tool`), against the `plaud` connection already
configured in Hermes' own MCP server config.

Every call this module can ever make is one of exactly three fixed tool
names, checked against `ALLOWED_PLAUD_TOOLS` before the connected-server
lookup even happens. `get_note`, Plaud summary, outline, Ask Plaud, template
notes, highlights, and mind-map surfaces are not merely avoided by
convention -- there is no code path in this module capable of naming one of
those tools, and any attempt to do so (e.g. a future edit that widens the
allowlist by mistake) is rejected before any MCP round-trip.
"""

from __future__ import annotations

import json
from typing import Any

from .plaud_transcript import TranscriptPage, TranscriptSegment

#: The connection name this build expects to already be configured in
#: Hermes' `mcp_servers` config. Not configurable via argument.
PLAUD_MCP_SERVER_NAME = "plaud"

#: The fixed, exact, three-entry tool allowlist. Final, never widened from
#: inside this module -- widening it requires a new, separately authorized
#: plan naming the exact tool and its approved use.
TOOL_LIST_RECORDINGS = "list_recordings"
TOOL_GET_RECORDING = "get_recording"
TOOL_GET_TRANSCRIPT = "get_transcript"
ALLOWED_PLAUD_TOOLS: frozenset[str] = frozenset(
    {TOOL_LIST_RECORDINGS, TOOL_GET_RECORDING, TOOL_GET_TRANSCRIPT}
)

#: Fixed per-call timeout, in seconds. Not configurable.
_CALL_TIMEOUT_S = 30.0


class PlaudMcpToolError(RuntimeError):
    """Raised for every LivePlaudTransport failure path: not connected, an
    MCP-level error result, or unparseable output. The message never
    carries tool output content -- only a fixed classification string."""


class DisallowedPlaudToolError(RuntimeError):
    """Raised if *tool_name* is not one of the exact three allowlisted Plaud
    tools. This can only be reached by a code change inside this module
    itself -- there is no argument or config value that selects the tool
    name from outside it."""


def _normalize_duration(item: dict[str, Any]) -> dict[str, Any]:
    """Plaud's MCP contract may report duration in milliseconds. Convert to
    whole seconds here, once, so every downstream consumer (normalize_record
    in plaud_collector.py, plaud_match.py) only ever sees `duration_s`."""
    if "duration_s" in item and item["duration_s"] is not None:
        return item
    out = dict(item)
    duration_ms = out.pop("duration_ms", None)
    if duration_ms is not None:
        out["duration_s"] = int(round(duration_ms / 1000))
    return out


def _extract_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


class LivePlaudTransport:
    """Production `PlaudTransport` + `PlaudTranscriptTransport`.

    Talks to the `plaud` MCP connection through the existing connected-server
    registry (`tools.mcp_tool`), never a transport this module opens itself.
    Nothing in this build's own test suite constructs this against a real
    connection -- tests only ever inject a fake transport or monkeypatch the
    two private helpers this class calls.
    """

    def __init__(
        self, *, server_name: str = PLAUD_MCP_SERVER_NAME, timeout: float = _CALL_TIMEOUT_S
    ) -> None:
        self._server_name = server_name
        self._timeout = timeout

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name not in ALLOWED_PLAUD_TOOLS:
            raise DisallowedPlaudToolError(
                f"Refusing to call Plaud MCP tool {tool_name!r}: not in the fixed allowlist."
            )

        from tools.mcp_tool import _get_connected_server_for_call, _run_on_mcp_loop

        server = _get_connected_server_for_call(self._server_name)
        if server is None or getattr(server, "session", None) is None:
            raise PlaudMcpToolError(
                f"Plaud MCP server {self._server_name!r} is not connected."
            )

        async def _call_tool():
            return await server.session.call_tool(tool_name, arguments=arguments)

        try:
            result = _run_on_mcp_loop(_call_tool, timeout=self._timeout)
        except Exception as exc:
            raise PlaudMcpToolError(
                f"Plaud MCP tool call failed [{type(exc).__name__}]."
            ) from None

        if _is_error(result):
            raise PlaudMcpToolError(f"Plaud MCP tool {tool_name!r} returned an error.")

        text = _extract_text(result)
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            raise PlaudMcpToolError(
                f"Plaud MCP tool {tool_name!r} returned unparseable output."
            ) from None

    # --- PlaudTransport (metadata only; see plaud_collector.py) -------------

    def get_deployment_boundary(self) -> str:
        payload = self._call(TOOL_LIST_RECORDINGS, {"since": None, "boundary_only": True})
        boundary = payload.get("boundary") if isinstance(payload, dict) else None
        if not boundary:
            raise PlaudMcpToolError("Plaud MCP tool returned no deployment boundary.")
        return str(boundary)

    def fetch_records_since(self, checkpoint: str | None) -> list[dict[str, Any]]:
        payload = self._call(TOOL_LIST_RECORDINGS, {"since": checkpoint})
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return []
        return [_normalize_duration(r) for r in records if isinstance(r, dict)]

    def fetch_record_by_identity(self, recording_id: str) -> dict[str, Any] | None:
        payload = self._call(TOOL_GET_RECORDING, {"recording_id": recording_id})
        if not isinstance(payload, dict):
            return None
        record = payload.get("record") if "record" in payload else payload
        if not isinstance(record, dict) or not record:
            return None
        return _normalize_duration(record)

    # --- PlaudTranscriptTransport (transaction transcript only) -------------

    def fetch_transcript_page(self, recording_id: str, cursor: str | None) -> TranscriptPage:
        payload = self._call(
            TOOL_GET_TRANSCRIPT,
            {"recording_id": recording_id, "cursor": cursor, "type": "transaction"},
        )
        if not isinstance(payload, dict):
            raise PlaudMcpToolError(
                f"Plaud MCP tool {TOOL_GET_TRANSCRIPT!r} returned a malformed page."
            )
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            raise PlaudMcpToolError(
                f"Plaud MCP tool {TOOL_GET_TRANSCRIPT!r} returned a malformed page."
            )
        segments = [
            TranscriptSegment(speaker=s.get("speaker"), text=str(s.get("text") or ""))
            for s in raw_segments
            if isinstance(s, dict)
        ]
        next_cursor = payload.get("next_cursor")
        return TranscriptPage(
            segments=segments, next_cursor=str(next_cursor) if next_cursor else None
        )


__all__ = [
    "PLAUD_MCP_SERVER_NAME",
    "TOOL_LIST_RECORDINGS",
    "TOOL_GET_RECORDING",
    "TOOL_GET_TRANSCRIPT",
    "ALLOWED_PLAUD_TOOLS",
    "PlaudMcpToolError",
    "DisallowedPlaudToolError",
    "LivePlaudTransport",
]
