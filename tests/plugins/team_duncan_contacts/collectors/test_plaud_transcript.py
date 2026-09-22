"""Tests for OPS-110 bounded, paginated Plaud transcript fetch."""

from __future__ import annotations

import pytest

from plugins.team_duncan_contacts.collectors.plaud_transcript import (
    MAX_TRANSCRIPT_BYTES,
    MAX_TRANSCRIPT_PAGES,
    MAX_TRANSCRIPT_SEGMENTS,
    TranscriptOverflowError,
    TranscriptPage,
    TranscriptSegment,
    fetch_full_transcript,
)


class FakeTranscriptTransport:
    def __init__(self, pages: list[TranscriptPage]) -> None:
        self._pages = pages
        self.requested_cursors: list[str | None] = []

    def fetch_transcript_page(self, recording_id: str, cursor: str | None) -> TranscriptPage:
        self.requested_cursors.append(cursor)
        return self._pages[len(self.requested_cursors) - 1]


class InfinitePageTransport:
    """Never reports a terminal cursor -- proves the fixed page-count bound
    protects against a misbehaving/malicious transport."""

    def fetch_transcript_page(self, recording_id: str, cursor: str | None) -> TranscriptPage:
        return TranscriptPage(segments=[], next_cursor="always-more")


def test_single_page_terminal_cursor() -> None:
    transport = FakeTranscriptTransport(
        [TranscriptPage(segments=[TranscriptSegment(speaker="A", text="hello")], next_cursor=None)]
    )
    result = fetch_full_transcript(transport, "rec-1")
    assert [s.text for s in result.segments] == ["hello"]
    assert result.page_count == 1
    assert transport.requested_cursors == [None]


def test_pagination_follows_next_cursor_until_none() -> None:
    transport = FakeTranscriptTransport(
        [
            TranscriptPage(segments=[TranscriptSegment(speaker="A", text="one")], next_cursor="c1"),
            TranscriptPage(segments=[TranscriptSegment(speaker="B", text="two")], next_cursor="c2"),
            TranscriptPage(segments=[TranscriptSegment(speaker="A", text="three")], next_cursor=None),
        ]
    )
    result = fetch_full_transcript(transport, "rec-1")
    assert [s.text for s in result.segments] == ["one", "two", "three"]
    assert transport.requested_cursors == [None, "c1", "c2"]
    assert result.page_count == 3


def test_segment_count_overflow_raises_and_fetches_nothing_usable() -> None:
    big_page = TranscriptPage(
        segments=[TranscriptSegment(speaker="A", text="x") for _ in range(MAX_TRANSCRIPT_SEGMENTS + 1)],
        next_cursor=None,
    )
    transport = FakeTranscriptTransport([big_page])
    with pytest.raises(TranscriptOverflowError):
        fetch_full_transcript(transport, "rec-1")


def test_byte_size_overflow_raises() -> None:
    huge_text = "x" * (MAX_TRANSCRIPT_BYTES + 1)
    page = TranscriptPage(segments=[TranscriptSegment(speaker="A", text=huge_text)], next_cursor=None)
    transport = FakeTranscriptTransport([page])
    with pytest.raises(TranscriptOverflowError):
        fetch_full_transcript(transport, "rec-1")


def test_page_count_overflow_raises_for_a_never_terminating_transport() -> None:
    with pytest.raises(TranscriptOverflowError):
        fetch_full_transcript(InfinitePageTransport(), "rec-1")


def test_bounds_are_within_page_limit_not_exceeded() -> None:
    assert MAX_TRANSCRIPT_PAGES > 0


def test_start_and_end_time_metadata_survive_the_bounded_fetch() -> None:
    transport = FakeTranscriptTransport(
        [TranscriptPage(
            segments=[TranscriptSegment(speaker="A", text="hello", start_time=0.0, end_time=1.2)],
            next_cursor=None,
        )]
    )
    result = fetch_full_transcript(transport, "rec-1")
    assert result.segments[0].start_time == 0.0
    assert result.segments[0].end_time == 1.2
