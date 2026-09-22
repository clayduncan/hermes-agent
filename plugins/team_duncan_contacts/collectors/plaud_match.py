"""OPS-110 Plaud-to-Desk metadata correlation.

Matches one Plaud recording to one immutable Desk call event using metadata
only: start-time and duration tolerance windows. Both tolerances are hard
filters, never a score or a tie-breaker -- there is no "closest" match, only
"the unique candidate that survives both filters" or a visible zero/multiple
outcome. Never reads caller identity, transcript, title, or any Plaud/Desk
content field to disambiguate.

Association identity is the pair (plaud recording_id, desk source_event_id).
This module never stores or returns a time window as identity -- only the
Desk record chosen (if unique) carries the stable source_event_id a caller
should persist.
"""

from __future__ import annotations

from dataclasses import dataclass

from .call_history_collector import DeskCallRecord
from .plaud_collector import PlaudRecord

#: Inclusive +/- tolerance on start time, in seconds. A Plaud recording may
#: begin before the Desk call connects (dialing/ringing lead-in), so the
#: window is symmetric and generous enough to absorb that, but fixed and
#: never widened per-call.
START_TOLERANCE_SECONDS = 120

#: Inclusive +/- tolerance on duration, in seconds. Absorbs minor rounding
#: between the two independent sources. Never used to break a tie -- see
#: module docstring.
DURATION_TOLERANCE_SECONDS = 3

MATCH_STATUS_MATCHED = "matched"
MATCH_STATUS_UNMATCHED = "unmatched"
MATCH_STATUS_AMBIGUOUS = "ambiguous"


@dataclass
class CorrelationResult:
    status: str  # 'matched' | 'unmatched' | 'ambiguous'
    desk_record: DeskCallRecord | None = None
    candidate_count: int = 0


def _within_tolerance(plaud: PlaudRecord, desk: DeskCallRecord) -> bool:
    """Both filters must pass. Neither is scored or compared to any other
    candidate's fit -- this is a pure inclusive-boundary predicate."""
    start_delta = abs((desk.occurred_at - plaud.occurred_at).total_seconds())
    if start_delta > START_TOLERANCE_SECONDS:
        return False

    if plaud.duration_s is None or desk.duration_s is None:
        # Duration data is required to confirm the match; its absence is
        # never treated as "no constraint" (which would let the start-time
        # filter alone decide -- effectively turning it into a tie-breaker).
        return False

    duration_delta = abs(desk.duration_s - plaud.duration_s)
    return duration_delta <= DURATION_TOLERANCE_SECONDS


def correlate_plaud_to_desk(
    plaud: PlaudRecord, desk_records: list[DeskCallRecord]
) -> CorrelationResult:
    """Filter *desk_records* down to those within both tolerance windows of
    *plaud*. Zero survivors -> unmatched. Exactly one -> matched. More than
    one -> ambiguous, with no heuristic winner ever selected."""
    candidates = [d for d in desk_records if _within_tolerance(plaud, d)]

    if len(candidates) == 0:
        return CorrelationResult(status=MATCH_STATUS_UNMATCHED, candidate_count=0)
    if len(candidates) > 1:
        return CorrelationResult(
            status=MATCH_STATUS_AMBIGUOUS, candidate_count=len(candidates)
        )
    return CorrelationResult(
        status=MATCH_STATUS_MATCHED, desk_record=candidates[0], candidate_count=1
    )


__all__ = [
    "START_TOLERANCE_SECONDS",
    "DURATION_TOLERANCE_SECONDS",
    "MATCH_STATUS_MATCHED",
    "MATCH_STATUS_UNMATCHED",
    "MATCH_STATUS_AMBIGUOUS",
    "CorrelationResult",
    "correlate_plaud_to_desk",
]
