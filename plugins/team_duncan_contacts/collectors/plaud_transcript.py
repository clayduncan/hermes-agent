"""OPS-110 Plaud transcript fetch: bounded, paginated, transaction-only.

Transcript content is only ever reached after a unique metadata match
(plaud_match.py) and an activated-contact admission -- this module has no
opinion on that gate and is never called before it by the runner. What this
module *does* enforce on its own: only the Plaud `transaction` transcript
page tool is ever requested (never a note, summary, outline, template, Ask
Plaud, highlight, or mind-map surface -- see live_plaud_transport.py's fixed
allowlist, which this module's caller must be built from), and the fetch is
bounded in both segment count and total byte size. Pagination continues
until the transport reports `next_cursor is None`; an oversized transcript
fails visibly (raises) and the caller writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

#: Fixed bounds. Not configurable via argument, environment variable, or
#: runtime option -- widening these requires a new, separately authorized
#: plan, matching the precedent in call_history_collector.py.
MAX_TRANSCRIPT_SEGMENTS = 5000
MAX_TRANSCRIPT_BYTES = 2_000_000

#: Fixed pagination bound: even a well-formed transport that never reports
#: `next_cursor is None` cannot loop this module forever.
MAX_TRANSCRIPT_PAGES = 500


class TranscriptOverflowError(RuntimeError):
    """Raised when a transcript would exceed MAX_TRANSCRIPT_SEGMENTS,
    MAX_TRANSCRIPT_BYTES, or MAX_TRANSCRIPT_PAGES. The caller must treat this
    as a visible failure: no summary is generated and no note is written."""


@dataclass
class TranscriptSegment:
    speaker: str | None
    text: str
    start_time: Any = None
    end_time: Any = None

    @property
    def byte_size(self) -> int:
        return len((self.speaker or "").encode("utf-8")) + len(self.text.encode("utf-8"))


@dataclass
class TranscriptPage:
    segments: list[TranscriptSegment]
    next_cursor: str | None


class PlaudTranscriptTransport(Protocol):
    """Injectable transport boundary for the `transaction` transcript only.
    The live implementation talks to the Plaud MCP server through the
    existing native MCP client (live_plaud_transport.py); tests inject a
    fake. Never constructed automatically -- a caller must build and pass
    one explicitly."""

    def fetch_transcript_page(
        self, recording_id: str, cursor: str | None
    ) -> TranscriptPage:
        """Return one page of `transaction` transcript segments for
        *recording_id*, starting at *cursor* (None for the first page)."""
        ...


@dataclass
class TranscriptFetchResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    page_count: int = 0
    total_bytes: int = 0


def fetch_full_transcript(
    transport: PlaudTranscriptTransport, recording_id: str
) -> TranscriptFetchResult:
    """Paginate *transport* until `next_cursor` is None, enforcing the fixed
    bounds on every page. Raises TranscriptOverflowError -- never truncates
    silently -- the instant any bound would be exceeded."""
    segments: list[TranscriptSegment] = []
    total_bytes = 0
    cursor: str | None = None
    page_count = 0

    while True:
        page_count += 1
        if page_count > MAX_TRANSCRIPT_PAGES:
            raise TranscriptOverflowError(
                f"Plaud transcript for recording {recording_id!r} exceeded "
                f"{MAX_TRANSCRIPT_PAGES} pages without a terminal cursor."
            )

        page = transport.fetch_transcript_page(recording_id, cursor)

        for seg in page.segments:
            total_bytes += seg.byte_size
            if total_bytes > MAX_TRANSCRIPT_BYTES:
                raise TranscriptOverflowError(
                    f"Plaud transcript for recording {recording_id!r} exceeded "
                    f"{MAX_TRANSCRIPT_BYTES} bytes."
                )
            segments.append(seg)
            if len(segments) > MAX_TRANSCRIPT_SEGMENTS:
                raise TranscriptOverflowError(
                    f"Plaud transcript for recording {recording_id!r} exceeded "
                    f"{MAX_TRANSCRIPT_SEGMENTS} segments."
                )

        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    return TranscriptFetchResult(
        segments=segments, page_count=page_count, total_bytes=total_bytes
    )


__all__ = [
    "MAX_TRANSCRIPT_SEGMENTS",
    "MAX_TRANSCRIPT_BYTES",
    "MAX_TRANSCRIPT_PAGES",
    "TranscriptOverflowError",
    "TranscriptSegment",
    "TranscriptPage",
    "PlaudTranscriptTransport",
    "TranscriptFetchResult",
    "fetch_full_transcript",
]
