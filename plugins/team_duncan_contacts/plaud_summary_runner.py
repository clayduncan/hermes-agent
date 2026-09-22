"""OPS-110 Plaud call-summary orchestration.

Separate from (and never touching) the OPS-18 IngestionRunner/desk_call
pipeline: this runner has its own source_cursors row
(SOURCE_CURSOR_KEY = "plaud_summary"), its own durable per-recording state
table (plaud_summary_state), and its own manual-run gate tools
(prepare_plaud_summary_run / confirm_plaud_summary_run in tools.py). It
reuses -- never duplicates -- the registry's identity gate, the activity
ledger's admission, the Desk collector's sealed re-fetch, and the note_mirror
table's dedupe identity.

Per Plaud recording, in order:
  1. Metadata-only correlation against a Desk call (plaud_match.py). Zero or
     multiple candidates is a terminal, visible, no-op outcome: no ledger
     event, no transcript fetch, no note.
  2. Sealed re-fetch of the exact matched Desk event (never the routine-scan
     copy) and identity resolution through registry.resolve_event using
     *only* that sealed Desk raw handle. Plaud's real metadata contract
     carries no caller-handle field at all; PlaudRecord.caller_handle is
     optional and this module never reads it -- identity comes solely from
     the Desk raw handle, and correlation (plaud_match.py) uses only
     PlaudRecord.occurred_at and .duration_s.
  3. Admission through the existing ActivityLedger (idempotent: a call
     already admitted by the Desk-only pipeline resolves to the same
     event_id and is a safe no-op re-affirmation). A non-activated or
     non-matching identity is skipped -- never queued for review.
  4. Transcript fetch (plaud_transcript.py), bounded and paginated, only
     ever the `transaction` surface.
  5. A bounded GHL contact context (plaud_context.py) and a claude-max
     subprocess summary (claude_summarizer.py). The visible note body is
     never Claude's free-form summary_lines -- it is deterministically
     composed here (build_visible_body_lines) from the structured
     discussed/clay_commitment/next_step fields, so Clay's commitment and
     the next step can never be silently dropped. Fewer than 2 resulting
     lines is treated as invalid summarizer output: no note is written and
     the frontier holds for retry, the same as a SummarizerError.
  6. A GHL note create-or-update-in-place (plaud_note_writer.py), title
     built from the same metadata-only formatter the Desk-only mirror uses.

A transcript or summarizer failure holds the cursor frontier at that record
for retry -- the same "no partial advance past a failure" discipline
ingestion_runner.py uses -- and never produces a duplicate note. A
non-activated/zero-match/ambiguous outcome is terminal and safe to advance
past. An already-terminal recording (found via plaud_summary_state) is
skipped immediately on a replay/overlapping scan without repeating any
transcript fetch, summarizer call, or GHL write.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .claude_summarizer import (
    SummarizerError,
    SummaryResult,
    build_visible_body_lines,
    run_claude_summary,
)
from .collectors.call_history_collector import (
    AmbiguousReplayError,
    CallHistoryCollector,
    DeskCallRecord,
    utc_to_apple_epoch,
)
from .collectors.plaud_collector import PlaudCollector, PlaudRecord
from .collectors.plaud_match import (
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    correlate_plaud_to_desk,
)
from .collectors.plaud_transcript import (
    PlaudTranscriptTransport,
    TranscriptOverflowError,
    fetch_full_transcript,
)
from .ingestion_state_db import (
    IngestionStateDb,
    PLAUD_SUMMARY_STATUS_COMPLETE,
    PLAUD_SUMMARY_STATUS_FAILED,
    PLAUD_TRANSCRIPT_STATUS_FAILED,
    PLAUD_TRANSCRIPT_STATUS_FETCHED,
    SOURCE_DESK_CALL,
)
from .note_mirror import format_note_body
from .plaud_context import build_bounded_contact_context
from .plaud_note_writer import PlaudSummaryNoteWriter

log = logging.getLogger(__name__)

#: Distinct from SOURCE_PLAUD/SOURCE_DESK_CALL: this runner's own cursor,
#: never read or written by IngestionRunner.
SOURCE_CURSOR_KEY = "plaud_summary"

#: Recorded in plaud_summary_state.error_class for a matched-but-not-
#: activated outcome. Not a failure -- a terminal, content-free marker so a
#: replay can skip it without holding the frontier for retry.
_NOT_ADMITTED_MARKER = "not_admitted"

#: Recorded in plaud_summary_state.error_class when Claude's structured
#: discussed/clay_commitment/next_step fields would produce fewer than 2
#: visible body lines. Treated exactly like a SummarizerError: no note is
#: written and the cursor frontier holds for retry.
_INVALID_STRUCTURED_SUMMARY = "invalid_structured_summary"

_ADMITTED_OUTCOMES = frozenset({"admitted", "override_admitted"})
_OK_NOTE_OUTCOMES = frozenset({"created", "updated", "already_current"})


class SourceMissingError(RuntimeError):
    pass


class SourceAmbiguousError(RuntimeError):
    pass


@dataclass
class PlaudSummaryRunSummary:
    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    skipped_not_activated: int = 0
    skipped_already_processed: int = 0
    notes_written: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "unmatched": self.unmatched,
            "ambiguous": self.ambiguous,
            "skipped_not_activated": self.skipped_not_activated,
            "skipped_already_processed": self.skipped_already_processed,
            "notes_written": self.notes_written,
            "errors": self.errors,
        }


def _is_terminal(row: Any) -> bool:
    if row is None:
        return False
    if row.match_status in (MATCH_STATUS_UNMATCHED, MATCH_STATUS_AMBIGUOUS):
        return True
    if row.note_id:
        return True
    if row.error_class == _NOT_ADMITTED_MARKER:
        return True
    return False


class PlaudSummaryRunner:
    def __init__(
        self,
        *,
        registry: Any,
        activity_ledger: Any,
        state_db: IngestionStateDb,
        plaud_collector: PlaudCollector,
        transcript_transport: PlaudTranscriptTransport,
        desk_collector: CallHistoryCollector,
        ghl_reader: Any,
        note_writer: PlaudSummaryNoteWriter,
        hermes_home: Path,
        summarizer: Callable[..., SummaryResult] = run_claude_summary,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._registry = registry
        self._activity_ledger = activity_ledger
        self._state_db = state_db
        self._plaud = plaud_collector
        self._transcript_transport = transcript_transport
        self._desk = desk_collector
        self._ghl_reader = ghl_reader
        self._note_writer = note_writer
        self._hermes_home = hermes_home
        self._summarizer = summarizer
        self._clock = clock

    def run(self, *, token: str | None = None) -> PlaudSummaryRunSummary:
        summary = PlaudSummaryRunSummary()

        checkpoint = self._state_db.get_cursor(SOURCE_CURSOR_KEY)
        try:
            if checkpoint is None:
                checkpoint = self._plaud.initialize_cursor()
                self._state_db.set_cursor(SOURCE_CURSOR_KEY, checkpoint)
            records = self._plaud.fetch_new(checkpoint)
        except Exception as exc:
            log.error("Plaud metadata fetch failed [%s]; cursor not advanced.", type(exc).__name__)
            summary.errors += 1
            return summary

        now = self._clock()
        try:
            desk_records = self._desk.fetch_routine_window(now)
        except Exception as exc:
            log.error("Desk fetch failed [%s]; cursor not advanced.", type(exc).__name__)
            summary.errors += 1
            return summary

        frontier = checkpoint
        frontier_broken = False
        for record in records:
            existing = self._state_db.get_plaud_summary_state(record.source_event_id)
            if _is_terminal(existing):
                summary.skipped_already_processed += 1
                reached = True
            else:
                try:
                    reached = self._process_record(record, desk_records, summary)
                except Exception as exc:
                    log.error(
                        "Error processing Plaud recording [%s]; batch continues.",
                        type(exc).__name__,
                    )
                    summary.errors += 1
                    reached = False

            if reached and not frontier_broken:
                frontier = record.position
                self._state_db.set_cursor(SOURCE_CURSOR_KEY, frontier)
            else:
                frontier_broken = True

        return summary

    def _sealed_refetch_desk(self, desk_record: DeskCallRecord) -> DeskCallRecord:
        target_zdate = utc_to_apple_epoch(desk_record.occurred_at)
        zoriginated = 1 if desk_record.direction == "outbound" else 0
        zanswered = desk_record.answered if desk_record.answered is not None else 0
        try:
            record = self._desk.fetch_exact_event(
                target_zdate=target_zdate,
                zoriginated=zoriginated,
                zanswered=zanswered,
                duration_s=desk_record.duration_s,
                expected_source_event_id=desk_record.source_event_id,
            )
        except AmbiguousReplayError:
            raise SourceAmbiguousError(desk_record.source_event_id) from None
        if record is None:
            raise SourceMissingError(desk_record.source_event_id)
        return record

    def _process_record(
        self,
        record: PlaudRecord,
        desk_records: list[DeskCallRecord],
        summary: PlaudSummaryRunSummary,
    ) -> bool:
        """Returns True iff this Plaud recording reached a terminal, safe
        outcome the cursor may pass. False holds the frontier for retry."""
        plaud_recording_id = record.source_event_id

        correlation = correlate_plaud_to_desk(record, desk_records)

        if correlation.status == MATCH_STATUS_UNMATCHED:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_UNMATCHED, error_class=None,
            )
            summary.unmatched += 1
            return True

        if correlation.status == MATCH_STATUS_AMBIGUOUS:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_AMBIGUOUS, error_class=None,
            )
            summary.ambiguous += 1
            return True

        desk_record = correlation.desk_record
        desk_source_event_id = desk_record.source_event_id
        self._state_db.upsert_plaud_summary_state(
            plaud_recording_id,
            match_status=MATCH_STATUS_MATCHED,
            desk_source_event_id=desk_source_event_id,
            error_class=None,
        )
        summary.matched += 1

        try:
            sealed_desk = self._sealed_refetch_desk(desk_record)
        except (SourceMissingError, SourceAmbiguousError) as exc:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                error_class=type(exc).__name__,
            )
            summary.errors += 1
            return False

        raw_handle = sealed_desk.raw_handle
        gate = self._registry.resolve_event(
            raw_handle, sealed_desk.occurred_at,
            source=SOURCE_DESK_CALL, source_event_id=desk_source_event_id,
        )
        contact_id = gate.ghl_contact_id

        ledger_result = self._activity_ledger.record_event(
            SOURCE_DESK_CALL, desk_source_event_id, raw_handle,
            sealed_desk.occurred_at, sealed_desk.provenance(),
        )
        raw_handle = None

        if ledger_result.outcome not in _ADMITTED_OUTCOMES:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                error_class=_NOT_ADMITTED_MARKER,
            )
            summary.skipped_not_activated += 1
            return True

        self._state_db.upsert_plaud_summary_state(
            plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
            contact_id=contact_id, error_class=None,
        )

        try:
            fetch_result = fetch_full_transcript(self._transcript_transport, plaud_recording_id)
        except TranscriptOverflowError:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                transcript_status=PLAUD_TRANSCRIPT_STATUS_FAILED,
                error_class="transcript_overflow",
            )
            summary.errors += 1
            return False
        except Exception:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                transcript_status=PLAUD_TRANSCRIPT_STATUS_FAILED,
                error_class="transcript_fetch_failed",
            )
            summary.errors += 1
            return False

        self._state_db.upsert_plaud_summary_state(
            plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
            transcript_status=PLAUD_TRANSCRIPT_STATUS_FETCHED, error_class=None,
        )

        contact = self._ghl_reader.get_contact_by_id(contact_id)
        if contact is None:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                summary_status=PLAUD_SUMMARY_STATUS_FAILED, error_class="contact_not_found",
            )
            summary.errors += 1
            return False
        context = build_bounded_contact_context(contact)
        contact = None

        transcript_payload = [
            {"speaker": seg.speaker, "text": seg.text} for seg in fetch_result.segments
        ]

        try:
            result = self._summarizer(
                transcript_segments=transcript_payload,
                contact_context=context,
                hermes_home=self._hermes_home,
            )
        except SummarizerError as exc:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                summary_status=PLAUD_SUMMARY_STATUS_FAILED, error_class=exc.error_class,
            )
            summary.errors += 1
            return False
        finally:
            transcript_payload = None

        visible_body_lines = build_visible_body_lines(result)
        if visible_body_lines is None:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                summary_status=PLAUD_SUMMARY_STATUS_FAILED,
                error_class=_INVALID_STRUCTURED_SUMMARY,
            )
            summary.errors += 1
            return False

        claude_output_hash = hashlib.sha256(
            json.dumps(asdict(result), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self._state_db.upsert_plaud_summary_state(
            plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
            summary_status=PLAUD_SUMMARY_STATUS_COMPLETE,
            claude_output_hash=claude_output_hash, error_class=None,
        )

        title = format_note_body(
            direction=sealed_desk.direction,
            answered=sealed_desk.answered,
            duration_s=sealed_desk.duration_s,
        )

        note_result = self._note_writer.write(
            desk_source_event_id=desk_source_event_id,
            contact_id=contact_id,
            title=title,
            summary_lines=visible_body_lines,
            trigger=(
                f"OPS-110 plaud summary for recording {plaud_recording_id} "
                f"/ desk event {desk_source_event_id}"
            ),
        )

        if note_result.outcome not in _OK_NOTE_OUTCOMES:
            self._state_db.upsert_plaud_summary_state(
                plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
                error_class="note_write_failed",
            )
            summary.errors += 1
            return False

        self._state_db.upsert_plaud_summary_state(
            plaud_recording_id, match_status=MATCH_STATUS_MATCHED,
            note_id=note_result.note_id, error_class=None,
        )
        summary.notes_written += 1
        return True


__all__ = [
    "SOURCE_CURSOR_KEY",
    "SourceMissingError",
    "SourceAmbiguousError",
    "PlaudSummaryRunSummary",
    "PlaudSummaryRunner",
]
