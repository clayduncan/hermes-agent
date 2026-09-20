"""Plaud MCP metadata collector.

Reads only identity/timing fields, the caller handle needed for one-shot
resolve_event matching, and transcript/summary availability flags. Never
fetches transcript text, summary text, or mind-map content, under any
circumstance -- including as a fallback when an availability flag's shape
can't be determined (that case resolves to None/"unknown" instead).

Cursor starts at the deployment boundary on first run: no historical
backfill, ever, regardless of argument or option.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from ..ingestion_state_db import SOURCE_PLAUD


class PlaudTransport(Protocol):
    """Injectable transport boundary. The live implementation talks to the
    Plaud MCP server; tests inject a fake. Never constructed automatically
    by this module -- a caller must build and pass one explicitly."""

    def get_deployment_boundary(self) -> str:
        """Return the forward-cursor/newest-record boundary the MCP contract
        exposes. Used exactly once, at first-run cursor initialization."""
        ...

    def fetch_records_since(self, checkpoint: str | None) -> list[dict[str, Any]]:
        """Return raw MCP records at or after *checkpoint*."""
        ...

    def fetch_record_by_identity(self, recording_id: str) -> dict[str, Any] | None:
        """Point lookup for sealed re-fetch/replay. None if not found."""
        ...


@dataclass
class PlaudRecord:
    source: str
    source_event_id: str
    occurred_at: datetime
    duration_s: int | None
    caller_handle: str  # raw handle; caller must clear it after resolve_event
    transcript_available: bool | None
    summary_available: bool | None

    @property
    def position(self) -> str:
        return self.occurred_at.isoformat()

    def provenance(self) -> dict[str, Any]:
        """The exact approved provenance projection. No raw handle, no content."""
        return {
            "plaud_recording_id": self.source_event_id,
            "duration_s": self.duration_s,
            "transcript_available": self.transcript_available,
            "summary_available": self.summary_available,
        }


def _coerce_availability(value: Any) -> bool | None:
    """Accept exactly two shapes: an explicit bool, or a non-negative int
    content-length (0 = absent, positive = present). Anything else is
    unknown -- never a reason to fetch content to find out."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value > 0
    return None


def normalize_record(raw: dict[str, Any]) -> PlaudRecord:
    if "mind_map" in raw or "mindmap" in raw:
        raise ValueError(
            "Plaud record carries a mind-map field; rejected at read time."
        )
    recording_id = raw.get("recording_id")
    if not recording_id:
        raise ValueError("Plaud record is missing its stable identity field (recording_id).")

    start_time = raw.get("start_time")
    if isinstance(start_time, datetime):
        occurred_at = start_time
    elif isinstance(start_time, str):
        occurred_at = datetime.fromisoformat(start_time)
    else:
        raise ValueError("Plaud record is missing a usable start_time.")
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    caller_handle = raw.get("caller_handle")
    if not caller_handle:
        raise ValueError("Plaud record is missing its caller_handle for resolution.")

    return PlaudRecord(
        source=SOURCE_PLAUD,
        source_event_id=str(recording_id),
        occurred_at=occurred_at,
        duration_s=raw.get("duration_s"),
        caller_handle=str(caller_handle),
        transcript_available=_coerce_availability(raw.get("transcript_available")),
        summary_available=_coerce_availability(raw.get("summary_available")),
    )


class PlaudCollector:
    """Normalizes Plaud MCP metadata records via an injected transport."""

    def __init__(self, transport: PlaudTransport) -> None:
        self._transport = transport

    def initialize_cursor(self) -> str:
        """Deployment-boundary cursor init. Only called when no
        source_cursors row exists yet for SOURCE_PLAUD; never reads
        historical content to derive the boundary."""
        return self._transport.get_deployment_boundary()

    def fetch_new(self, checkpoint: str | None) -> list[PlaudRecord]:
        raw_records = self._transport.fetch_records_since(checkpoint)
        return [normalize_record(r) for r in raw_records]

    def fetch_by_identity(self, recording_id: str) -> PlaudRecord | None:
        """Sealed exact-source re-fetch, keyed by the immutable recording_id.
        Used for the selection-time, creation, activation, and replay
        re-fetch moments."""
        raw = self._transport.fetch_record_by_identity(recording_id)
        if raw is None:
            return None
        return normalize_record(raw)
