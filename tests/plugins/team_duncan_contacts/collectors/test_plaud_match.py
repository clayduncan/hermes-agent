"""Tests for OPS-110 Plaud-to-Desk metadata correlation.

Pure functions only -- no I/O, no fakes needed beyond the two record
dataclasses themselves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from plugins.team_duncan_contacts.collectors.call_history_collector import DeskCallRecord
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudRecord
from plugins.team_duncan_contacts.collectors.plaud_match import (
    DURATION_TOLERANCE_SECONDS,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    START_TOLERANCE_SECONDS,
    correlate_plaud_to_desk,
)

DESK_START = datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc)


def _plaud(occurred_at=DESK_START, duration_s=1306, recording_id="rec-1") -> PlaudRecord:
    return PlaudRecord(
        source="plaud", source_event_id=recording_id, occurred_at=occurred_at,
        duration_s=duration_s, caller_handle="ignored-for-correlation",
        transcript_available=True, summary_available=False,
    )


def _desk(occurred_at=DESK_START, duration_s=1306, source_event_id="desk-1") -> DeskCallRecord:
    return DeskCallRecord(
        source="desk_call", source_event_id=source_event_id, occurred_at=occurred_at,
        duration_s=duration_s, raw_handle="+15551234567", direction="inbound", answered=1,
    )


class TestUniqueMatch:
    def test_exact_start_and_duration_matches(self) -> None:
        result = correlate_plaud_to_desk(_plaud(), [_desk()])
        assert result.status == MATCH_STATUS_MATCHED
        assert result.desk_record.source_event_id == "desk-1"
        assert result.candidate_count == 1

    def test_real_seam_fixture_matches(self) -> None:
        """The ticket's real-world seam: Plaud start 19:22:59 / duration
        1305s (1,305,000 ms) vs. Desk start 19:24:30.029607 / duration
        ~1306.196s. Confirms both tolerances accept it."""
        plaud_start = datetime(2026, 9, 17, 19, 22, 59, tzinfo=timezone.utc)
        desk_start = datetime(2026, 9, 17, 19, 24, 30, 29607, tzinfo=timezone.utc)
        plaud = _plaud(occurred_at=plaud_start, duration_s=1305, recording_id="of_43c744fd9e22636f06b4508908b73531")
        desk = _desk(occurred_at=desk_start, duration_s=1306, source_event_id="desk-cory")
        result = correlate_plaud_to_desk(plaud, [desk])
        assert result.status == MATCH_STATUS_MATCHED
        assert result.desk_record.source_event_id == "desk-cory"


class TestBoundaryInclusivity:
    def test_start_delta_exactly_at_tolerance_matches(self) -> None:
        desk = _desk(occurred_at=DESK_START + timedelta(seconds=START_TOLERANCE_SECONDS))
        result = correlate_plaud_to_desk(_plaud(), [desk])
        assert result.status == MATCH_STATUS_MATCHED

    def test_start_delta_one_second_past_tolerance_unmatches(self) -> None:
        desk = _desk(occurred_at=DESK_START + timedelta(seconds=START_TOLERANCE_SECONDS + 1))
        result = correlate_plaud_to_desk(_plaud(), [desk])
        assert result.status == MATCH_STATUS_UNMATCHED

    def test_start_delta_exactly_at_negative_tolerance_matches(self) -> None:
        desk = _desk(occurred_at=DESK_START - timedelta(seconds=START_TOLERANCE_SECONDS))
        result = correlate_plaud_to_desk(_plaud(), [desk])
        assert result.status == MATCH_STATUS_MATCHED

    def test_duration_delta_exactly_at_tolerance_matches(self) -> None:
        desk = _desk(duration_s=1306 + DURATION_TOLERANCE_SECONDS)
        result = correlate_plaud_to_desk(_plaud(duration_s=1306), [desk])
        assert result.status == MATCH_STATUS_MATCHED

    def test_duration_delta_one_second_past_tolerance_unmatches(self) -> None:
        desk = _desk(duration_s=1306 + DURATION_TOLERANCE_SECONDS + 1)
        result = correlate_plaud_to_desk(_plaud(duration_s=1306), [desk])
        assert result.status == MATCH_STATUS_UNMATCHED


class TestZeroAndMultipleCandidates:
    def test_no_desk_records_is_unmatched(self) -> None:
        result = correlate_plaud_to_desk(_plaud(), [])
        assert result.status == MATCH_STATUS_UNMATCHED
        assert result.desk_record is None
        assert result.candidate_count == 0

    def test_no_candidate_within_tolerance_is_unmatched(self) -> None:
        desk = _desk(occurred_at=DESK_START + timedelta(hours=1))
        result = correlate_plaud_to_desk(_plaud(), [desk])
        assert result.status == MATCH_STATUS_UNMATCHED

    def test_two_candidates_within_tolerance_is_ambiguous(self) -> None:
        desk_a = _desk(source_event_id="desk-a")
        desk_b = _desk(source_event_id="desk-b", occurred_at=DESK_START + timedelta(seconds=1))
        result = correlate_plaud_to_desk(_plaud(), [desk_a, desk_b])
        assert result.status == MATCH_STATUS_AMBIGUOUS
        assert result.desk_record is None
        assert result.candidate_count == 2

    def test_ambiguous_never_picks_a_heuristic_winner(self) -> None:
        """One candidate is a closer start-time match than the other, but
        both are within tolerance -- there is no scoring, so this is still
        ambiguous, never resolved to the 'closer' one."""
        desk_close = _desk(source_event_id="desk-close", occurred_at=DESK_START)
        desk_far = _desk(source_event_id="desk-far", occurred_at=DESK_START + timedelta(seconds=100))
        result = correlate_plaud_to_desk(_plaud(), [desk_close, desk_far])
        assert result.status == MATCH_STATUS_AMBIGUOUS
        assert result.desk_record is None


class TestDurationNeverTieBreaks:
    def test_two_candidates_in_start_window_one_with_better_duration_still_ambiguous(self) -> None:
        """Both candidates pass the duration filter (within tolerance);
        one happens to match duration exactly and the other doesn't quite
        as well. Duration is a filter, not a tie-breaker, so this stays
        ambiguous rather than picking the exact-duration one."""
        desk_exact_duration = _desk(source_event_id="desk-exact", duration_s=1306)
        desk_close_duration = _desk(
            source_event_id="desk-close", occurred_at=DESK_START + timedelta(seconds=5),
            duration_s=1306 + DURATION_TOLERANCE_SECONDS,
        )
        result = correlate_plaud_to_desk(_plaud(duration_s=1306), [desk_exact_duration, desk_close_duration])
        assert result.status == MATCH_STATUS_AMBIGUOUS

    def test_duration_outside_tolerance_excludes_a_candidate_from_ambiguity(self) -> None:
        """A candidate whose duration fails the filter is excluded outright
        -- proving duration is applied as a hard filter, not ignored."""
        desk_in_tolerance = _desk(source_event_id="desk-in", duration_s=1306)
        desk_out_of_tolerance = _desk(
            source_event_id="desk-out", occurred_at=DESK_START + timedelta(seconds=5),
            duration_s=1306 + DURATION_TOLERANCE_SECONDS + 50,
        )
        result = correlate_plaud_to_desk(
            _plaud(duration_s=1306), [desk_in_tolerance, desk_out_of_tolerance]
        )
        assert result.status == MATCH_STATUS_MATCHED
        assert result.desk_record.source_event_id == "desk-in"


class TestMissingDurationFailsClosed:
    def test_plaud_missing_duration_never_matches_on_start_time_alone(self) -> None:
        result = correlate_plaud_to_desk(_plaud(duration_s=None), [_desk()])
        assert result.status == MATCH_STATUS_UNMATCHED

    def test_desk_missing_duration_never_matches_on_start_time_alone(self) -> None:
        result = correlate_plaud_to_desk(_plaud(), [_desk(duration_s=None)])
        assert result.status == MATCH_STATUS_UNMATCHED
