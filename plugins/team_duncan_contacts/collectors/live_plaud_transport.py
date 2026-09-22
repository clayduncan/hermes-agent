"""OPS-110 production Plaud transport: Hermes' existing native MCP client.

This is the one place this build talks to a live Plaud connection. It does
not open a second credential store, scrape the Plaud UI, or call Plaud's
undocumented API directly -- it reuses the same connected-server registry
and background MCP event loop every other MCP tool call in this repo goes
through (`tools.mcp_tool`), against the `plaud` connection already
configured in Hermes' own MCP server config.

Every call this module can ever make is one of exactly two fixed tool
names -- `list_files` and `get_transcript`, the only tools actually
configured on the Plaud MCP connection -- checked against
`ALLOWED_PLAUD_TOOLS` before the connected-server lookup even happens.
`get_note`, Plaud summary, outline, Ask Plaud, template notes, highlights,
and mind-map surfaces are not merely avoided by convention -- there is no
code path in this module capable of naming one of those tools, and any
attempt to do so (e.g. a future edit that widens the allowlist by mistake)
is rejected before any MCP round-trip. Likewise, `get_transcript` is only
ever called with `block="transaction"`, hardcoded inside
`fetch_transcript_page` -- there is no parameter on this module's public
API that lets a caller request `outline`, `transaction_polish`, or
`mark_memo`.

Plaud has no by-ID lookup tool: every metadata read, including the
deployment-boundary cursor and an exact-ID sealed re-fetch, goes through
bounded, paginated `list_files` calls. There is no `query`/title-based
identity inference anywhere in this module -- the exact-ID re-fetch matches
only on the raw `id` field returned by `list_files`, never on `name`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .plaud_transcript import TranscriptPage, TranscriptSegment

#: The connection name this build expects to already be configured in
#: Hermes' `mcp_servers` config. Not configurable via argument.
PLAUD_MCP_SERVER_NAME = "plaud"

#: The fixed, exact, two-entry tool allowlist -- the only tools actually
#: configured on the Plaud MCP connection. Final, never widened from inside
#: this module -- widening it requires a new, separately authorized plan
#: naming the exact tool and its approved use.
TOOL_LIST_FILES = "list_files"
TOOL_GET_TRANSCRIPT = "get_transcript"
ALLOWED_PLAUD_TOOLS: frozenset[str] = frozenset({TOOL_LIST_FILES, TOOL_GET_TRANSCRIPT})

#: The only transcript block this module will ever request. Hardcoded into
#: `fetch_transcript_page` -- never a parameter. `outline`,
#: `transaction_polish`, and `mark_memo` are forbidden.
TRANSCRIPT_BLOCK = "transaction"

#: Fixed per-call timeout, in seconds. Not configurable.
_CALL_TIMEOUT_S = 30.0

#: Fixed `list_files` page size. The real contract requires a minimum of
#: 10; this build always asks for at least 100 to keep bounded scans short.
#: Not configurable.
LIST_FILES_PAGE_SIZE = 100

#: Fixed cap on the number of `list_files` pages any single bounded scan
#: (deployment boundary, forward metadata listing, or exact-ID re-fetch)
#: will walk. At LIST_FILES_PAGE_SIZE=100 this bounds a single scan to
#: 5,000 records. A scan that hits this cap without reaching its stopping
#: condition fails visibly -- it never silently returns a partial answer as
#: if it were complete.
MAX_LIST_FILES_PAGES = 50

#: Fixed `get_transcript` page size (contract max is 500). Not configurable.
TRANSCRIPT_PAGE_LIMIT = 500


class PlaudMcpToolError(RuntimeError):
    """Raised for every LivePlaudTransport failure path: not connected, an
    MCP-level error result, unparseable output, or a bounded scan that
    could not reach its stopping condition. The message never carries tool
    output content -- only a fixed classification string."""


class DisallowedPlaudToolError(RuntimeError):
    """Raised if *tool_name* is not one of the exact two allowlisted Plaud
    tools. This can only be reached by a code change inside this module
    itself -- there is no argument or config value that selects the tool
    name from outside it."""


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Plaud `start_at` value into an aware UTC datetime for
    comparison. Returns None for anything unparseable rather than raising --
    an unparseable timestamp makes a record unusable for cursor comparison,
    never a reason to guess."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_list_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Map one raw `list_files` entry (`id`, `name`, `created_at`,
    `serial_number`, `start_at`, `duration`) onto the normalized shape
    `PlaudTransport.fetch_*` callers expect. `id` is preserved verbatim as
    the immutable source identity; `name`/`created_at`/`serial_number` are
    dropped -- this build performs no title/query identity inference.
    `duration` (milliseconds) becomes `duration_s`. Plaud's `list_files`
    contract carries no availability flags, so those are always None."""
    recording_id = item.get("id")
    duration_ms = item.get("duration")
    duration_s = (
        int(round(duration_ms / 1000)) if isinstance(duration_ms, (int, float)) else None
    )
    return {
        "recording_id": str(recording_id) if recording_id is not None else None,
        "start_time": item.get("start_at"),
        "duration_s": duration_s,
        "transcript_available": None,
        "summary_available": None,
    }


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

    def _list_files_page(self, page: int) -> list[dict[str, Any]]:
        payload = self._call(
            TOOL_LIST_FILES, {"page": page, "page_size": LIST_FILES_PAGE_SIZE}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    # --- PlaudTransport (metadata only; see plaud_collector.py) -------------

    def get_deployment_boundary(self) -> str:
        """Forward metadata listing, bounded: walk up to
        MAX_LIST_FILES_PAGES pages of `list_files`, tracking the newest
        `start_at` seen. That newest timestamp becomes the first-run cursor,
        so no record that already existed at deployment time is ever
        fetched by `fetch_records_since`. An empty account falls back to
        the current time -- still no backfill."""
        newest: datetime | None = None
        page = 1
        while page <= MAX_LIST_FILES_PAGES:
            items = self._list_files_page(page)
            if not items:
                break
            for item in items:
                dt = _parse_dt(item.get("start_at"))
                if dt is not None and (newest is None or dt > newest):
                    newest = dt
            if len(items) < LIST_FILES_PAGE_SIZE:
                break
            page += 1
        if newest is None:
            newest = datetime.now(timezone.utc)
        return newest.isoformat()

    def fetch_records_since(self, checkpoint: str | None) -> list[dict[str, Any]]:
        """Forward metadata listing, bounded: walk `list_files` pages,
        collecting only records strictly newer than *checkpoint*. Stops as
        soon as a page contains no record newer than the checkpoint (the
        rest of the listing is older still) or the page/record cap is hit.
        No `query`/date filter is used -- this is a plain bounded page scan,
        never a title-based lookup."""
        if not checkpoint:
            return []
        checkpoint_dt = _parse_dt(checkpoint)
        if checkpoint_dt is None:
            return []

        collected: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_LIST_FILES_PAGES:
            items = self._list_files_page(page)
            if not items:
                break

            any_newer = False
            for item in items:
                dt = _parse_dt(item.get("start_at"))
                if dt is None or dt <= checkpoint_dt:
                    continue
                any_newer = True
                collected.append(_normalize_list_entry(item))

            if not any_newer:
                break
            if len(items) < LIST_FILES_PAGE_SIZE:
                break
            page += 1

        return collected

    def fetch_record_by_identity(self, recording_id: str) -> dict[str, Any] | None:
        """Sealed exact-ID re-fetch, bounded: walk `list_files` pages
        looking for the exact `id` -- never a `query`/name search. If the
        bounded scan exhausts (reaches the end of the listing, or the fixed
        page cap) without locating *recording_id*, this fails visibly rather
        than returning None as if non-existence were confirmed."""
        page = 1
        while page <= MAX_LIST_FILES_PAGES:
            items = self._list_files_page(page)
            if not items:
                break
            for item in items:
                if str(item.get("id")) == str(recording_id):
                    return _normalize_list_entry(item)
            if len(items) < LIST_FILES_PAGE_SIZE:
                break
            page += 1

        raise PlaudMcpToolError(
            f"Plaud MCP tool {TOOL_LIST_FILES!r} did not locate file_id "
            f"{recording_id!r} within the bounded scan."
        )

    # --- PlaudTranscriptTransport (transaction transcript only) -------------

    def fetch_transcript_page(self, recording_id: str, cursor: str | None) -> TranscriptPage:
        arguments: dict[str, Any] = {
            "file_id": recording_id,
            "block": TRANSCRIPT_BLOCK,
            "limit": TRANSCRIPT_PAGE_LIMIT,
        }
        if cursor is not None:
            arguments["cursor"] = cursor

        payload = self._call(TOOL_GET_TRANSCRIPT, arguments)
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
            TranscriptSegment(
                speaker=s.get("speaker"),
                text=str(s.get("content") or ""),
                start_time=s.get("start_time"),
                end_time=s.get("end_time"),
            )
            for s in raw_segments
            if isinstance(s, dict)
        ]
        next_cursor = payload.get("next_cursor")
        return TranscriptPage(
            segments=segments, next_cursor=str(next_cursor) if next_cursor else None
        )


__all__ = [
    "PLAUD_MCP_SERVER_NAME",
    "TOOL_LIST_FILES",
    "TOOL_GET_TRANSCRIPT",
    "TRANSCRIPT_BLOCK",
    "ALLOWED_PLAUD_TOOLS",
    "LIST_FILES_PAGE_SIZE",
    "MAX_LIST_FILES_PAGES",
    "TRANSCRIPT_PAGE_LIMIT",
    "PlaudMcpToolError",
    "DisallowedPlaudToolError",
    "LivePlaudTransport",
]
