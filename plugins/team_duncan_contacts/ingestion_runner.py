"""OPS-18 ingestion runner and review-workflow orchestration.

Routes every collected event through `registry.resolve_event`, admits only
through `ActivityLedger.record_event`, and drives the pending_review state
machine. Every privileged action here (contact creation, activation,
grant/replay, candidate selection) requires the exact Clay decision it
claims to require: either a single-use action-approval token, or (for
activation) the registry's own existing prepare/confirm token precedent.
Nothing in this module can be reached from a cron/background context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

from .collectors.call_history_collector import (
    AmbiguousReplayError,
    CallHistoryCollector,
    DeskCallRecord,
)
from .collectors.plaud_collector import PlaudCollector, PlaudRecord
from .ingestion_state_db import (
    ACTION_CREATE_CONTACT,
    ACTION_GRANT_OVERRIDE,
    IngestionStateDb,
    OUTCOME_DENY_PAUSED,
    OUTCOME_DENY_RETIRED,
    OUTCOME_UNEXPECTED_DECISION,
    PendingReviewRow,
    SOURCE_DESK_CALL,
    SOURCE_PLAUD,
    STATUS_AWAITING_ACTIVATION_CONFIRMATION,
    STATUS_CONTACT_ACTIVATED,
    STATUS_CONTACT_CREATED,
    STATUS_CONTACT_CREATION_PENDING,
    STATUS_CONTACT_SELECTED,
    STATUS_COMPLETE,
    STATUS_DISMISSED,
    STATUS_DUPLICATE_RESOLUTION_REQUIRED,
    STATUS_FAILED,
    STATUS_GRANT_ISSUED,
    STATUS_PENDING_REVIEW,
    STATUS_REPLAYED,
    STATUS_SOURCE_AMBIGUOUS,
    STATUS_SOURCE_MISSING,
    creation_idempotency_key,
)
from .notifications import (
    Notifier,
    build_collision_payload,
    build_deny_pre_activation_payload,
    build_multiple_match_payload,
    build_plaud_zero_match_payload,
)
from .note_mirror import OK_OUTCOMES as _NOTE_MIRROR_OK_OUTCOMES
from .note_mirror import contact_detail_url

log = logging.getLogger(__name__)

ALL_SOURCES: frozenset[str] = frozenset({SOURCE_PLAUD, SOURCE_DESK_CALL})


class WrongLocationError(RuntimeError):
    """A resolved event named a location other than Team Duncan's. Critical:
    stops only the affected source; the cursor for it does not advance."""


class RegistryUnavailableError(RuntimeError):
    """resolve_event could not complete a match (match_outcome=unavailable).
    Critical: stops only the affected source; the cursor does not advance."""


class SourceMissingError(RuntimeError):
    pass


class SourceAmbiguousError(RuntimeError):
    pass


class ActionNotApprovedError(RuntimeError):
    """The caller did not present a valid, unexpired, single-use approval
    token for this exact pending_review_id and action."""


def _mask_raw_handle(raw_handle: str) -> str:
    """Mask a raw handle (phone or email) for a Plaud zero-match notification.
    Applied to raw incoming data rather than a contact record -- the only
    context where that happens."""
    digit_count = sum(c.isdigit() for c in raw_handle)
    if digit_count >= 10:
        digits = re.sub(r"\D", "", raw_handle)
        return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***-***-****"
    if "@" in raw_handle:
        local, _, _domain = raw_handle.partition("@")
        return (local[:1] + "***@***.***") if local else "***@***.***"
    return "***"


@dataclass
class RunSummary:
    run_id: str | None
    admitted: int = 0
    override_admitted: int = 0
    discarded: int = 0
    pending_review: int = 0
    errors: int = 0
    critical_errors: int = 0
    critical_error_reasons: list[str] = field(default_factory=list)
    pending_review_count: int = 0
    oldest_pending_at: str | None = None
    failed_review_count: int = 0
    oldest_failed_at: str | None = None
    notes_created: int = 0
    notes_recovered: int = 0
    note_errors: int = 0
    note_references: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "admitted": self.admitted,
            "override_admitted": self.override_admitted,
            "discarded": self.discarded,
            "pending_review": self.pending_review,
            "errors": self.errors,
            "critical_errors": self.critical_errors,
            "pending_review_count": self.pending_review_count,
            "oldest_pending_at": self.oldest_pending_at,
            "failed_review_count": self.failed_review_count,
            "oldest_failed_at": self.oldest_failed_at,
            "notes_created": self.notes_created,
            "notes_recovered": self.notes_recovered,
            "note_errors": self.note_errors,
            "note_references": list(self.note_references),
        }


class IngestionRunner:
    def __init__(
        self,
        *,
        registry: Any,
        activity_ledger: Any,
        state_db: IngestionStateDb,
        plaud_collector: PlaudCollector,
        desk_collector: CallHistoryCollector,
        notifier: Notifier,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        enabled_sources: frozenset[str] | None = None,
        note_mirror: Any | None = None,
        defer_notifications: bool = False,
    ) -> None:
        """*enabled_sources* is a fixed internal configuration decided at
        construction time by the plugin factory -- never agent/model input.
        Defaults to both sources (pre-existing behavior) when omitted, so
        direct construction (as in most tests) is unaffected. A source not
        in *enabled_sources* is never read, fetched, or cursor-initialized:
        `run()` skips its `_run_*` step entirely, and due notification
        retries for that source are skipped too.

        *note_mirror* is the OPS-18 GHL note mirror (see note_mirror.py).
        When omitted (the default -- matching every pre-existing direct
        construction, mostly in tests), no note is ever written and every
        admitted/override-admitted event behaves exactly as before this
        feature existed. It is only ever consulted for SOURCE_DESK_CALL
        events -- Plaud stays excluded from the mirror the same way it stays
        excluded from ingestion, pending OPS-110.

        *defer_notifications* (OPS-114): when False (the default -- matching
        every pre-existing direct construction, mostly in tests), a genuine
        pending-review insertion sends through *notifier* and records the
        attempt on the row, and `run()` also drives the legacy due-retry
        loop, exactly as before this flag existed. When True (production
        wiring only, see `_build_ingestion_runner_factory`), neither send
        happens: no `notifier.send`, no `record_notification_attempt`, and
        `_process_due_retries` is never called. `notification_state` is left
        honestly at `not_notified` for every newly inserted row -- shared
        cron Amber (not this per-row Telegram/email path) is the production
        notification authority, so recording a false send attempt or a fake
        retry schedule here would misrepresent what actually happened.
        """
        self._registry = registry
        self._activity_ledger = activity_ledger
        self._state_db = state_db
        self._plaud = plaud_collector
        self._desk = desk_collector
        self._notifier = notifier
        self._clock = clock
        self._enabled_sources = (
            frozenset(enabled_sources) if enabled_sources is not None else ALL_SOURCES
        )
        self._note_mirror = note_mirror
        self._defer_notifications = defer_notifications

    # --- Ingestion ------------------------------------------------------------

    def run(self, *, token: str | None = None) -> RunSummary:
        """Fetch new records from each enabled source, route them, advance
        cursors contiguously, then attempt any due notification retries for
        enabled sources. Never advances a cursor past a failed event.
        Records this run's summary (with a run_id) iff *token* is given --
        the manual-run gate always supplies one; direct unit tests of
        routing logic may omit it. A disabled source is skipped outright:
        no cursor read, initialization, fetch, or error increment for it."""
        started_at = self._clock()
        summary = RunSummary(run_id=None)

        if SOURCE_PLAUD in self._enabled_sources:
            self._run_plaud(summary)
        if SOURCE_DESK_CALL in self._enabled_sources:
            self._run_desk(summary)
        if not self._defer_notifications:
            self._process_due_retries()

        summary.pending_review_count = self._state_db.count_unresolved_pending_review()
        summary.oldest_pending_at = self._state_db.oldest_pending_at()
        summary.failed_review_count = self._state_db.count_failed_pending_review()
        summary.oldest_failed_at = self._state_db.oldest_failed_at()

        if token is not None:
            completed_at = self._clock()
            summary.run_id = self._state_db.record_run(
                token=token,
                started_at=started_at,
                completed_at=completed_at,
                admitted=summary.admitted,
                override_admitted=summary.override_admitted,
                discarded=summary.discarded,
                pending_review_count=summary.pending_review,
                errors=summary.errors,
                critical_errors=summary.critical_errors,
            )
        return summary

    def _run_plaud(self, summary: RunSummary) -> None:
        checkpoint = self._state_db.get_cursor(SOURCE_PLAUD)
        try:
            if checkpoint is None:
                checkpoint = self._plaud.initialize_cursor()
                self._state_db.set_cursor(SOURCE_PLAUD, checkpoint)
            records = self._plaud.fetch_new(checkpoint)
        except Exception as exc:
            log.error(
                "Plaud source failed [%s]; source not advanced this run.",
                type(exc).__name__,
            )
            summary.errors += 1
            return
        self._drain(SOURCE_PLAUD, records, summary)

    def _run_desk(self, summary: RunSummary) -> None:
        now = self._clock()
        try:
            records = self._desk.fetch_routine_window(now)
        except Exception as exc:
            log.error(
                "Desk fetch failed [%s]; source not advanced this run.",
                type(exc).__name__,
            )
            summary.errors += 1
            return
        self._drain(SOURCE_DESK_CALL, records, summary)

    def _drain(
        self, source: str, records: list[PlaudRecord | DeskCallRecord], summary: RunSummary
    ) -> None:
        frontier = self._state_db.get_cursor(source)
        frontier_broken = False
        for record in records:
            try:
                reached = self._process_record(source, record, summary)
            except Exception as exc:
                # No traceback/message logged: a mid-record failure could in
                # principle carry a raw handle in its exception text before
                # this method's own `raw_handle = None` runs. Only the
                # exception type is safe to surface.
                log.error(
                    "Error processing %s record [%s]; batch continues.",
                    source, type(exc).__name__,
                )
                summary.errors += 1
                reached = False
            if reached and not frontier_broken:
                frontier = record.position
                self._state_db.set_cursor(source, frontier)
            else:
                frontier_broken = True

    def _process_record(
        self, source: str, record: PlaudRecord | DeskCallRecord, summary: RunSummary
    ) -> bool:
        """Returns True iff this record reached a terminal handoff (admitted,
        override-admitted, queued for review, or recorded as a processed
        non-actionable outcome) -- i.e. the frontier may safely pass it."""
        raw_handle = record.raw_handle if isinstance(record, DeskCallRecord) else record.caller_handle
        if not raw_handle:
            # Plaud's real metadata contract carries no caller-handle field at
            # all, so a Plaud record here has nothing to resolve identity
            # from. Fail visibly rather than resolving (or silently skipping)
            # on missing identity data -- this source is disabled in
            # production (see plugins/team_duncan_contacts/__init__.py
            # _ENABLED_SOURCES), so this path is unreached there.
            raise ValueError(f"{source} record has no raw handle to resolve identity from.")
        event_ts = record.occurred_at
        source_event_id = record.source_event_id
        provenance = record.provenance()
        duration_s = record.duration_s
        direction = record.direction if isinstance(record, DeskCallRecord) else None
        answered = record.answered if isinstance(record, DeskCallRecord) else None

        result = self._registry.resolve_event(
            raw_handle, event_ts, source=source, source_event_id=source_event_id
        )

        if result.location_id is not None and result.location_id != TEAM_DUNCAN_LOCATION_ID:
            raw_handle = None
            summary.critical_errors += 1
            summary.critical_error_reasons.append(f"wrong_location:{source}")
            return False

        if result.match_outcome == "unavailable":
            raw_handle = None
            summary.critical_errors += 1
            summary.critical_error_reasons.append(f"match_outcome_unavailable:{source}")
            return False

        decision = result.decision

        if decision == "allow":
            ledger_result = self._activity_ledger.record_event(
                source, source_event_id, raw_handle, event_ts, provenance
            )
            raw_handle = None
            if ledger_result.outcome == "admitted":
                summary.admitted += 1
                return self._mirror_note_for_admission(
                    source, ledger_result, result.ghl_contact_id, event_ts,
                    duration_s, direction, answered, summary,
                )
            summary.discarded += 1
            return True

        if decision == "deny_pre_activation":
            ledger_result = self._activity_ledger.record_event(
                source, source_event_id, raw_handle, event_ts, provenance
            )
            raw_handle = None
            if ledger_result.outcome == "override_admitted":
                summary.override_admitted += 1
                return self._mirror_note_for_admission(
                    source, ledger_result, result.ghl_contact_id, event_ts,
                    duration_s, direction, answered, summary,
                )

            masked_meta = result.masked_metadata
            ins = self._state_db.insert_pending_review(
                source=source, source_event_id=source_event_id, decision=decision,
                match_outcome=result.match_outcome, occurred_at=_iso(event_ts),
                duration_s=duration_s, direction=direction, answered=answered,
                status=STATUS_PENDING_REVIEW,
                display_name=masked_meta.get("display_name"),
                masked_labels=masked_meta.get("masked_labels"),
            )
            summary.pending_review += 1
            if ins.genuine_insert and not self._defer_notifications:
                row = self._state_db.get_pending_review(ins.pending_review_id)
                delivered = self._notifier.send(build_deny_pre_activation_payload(row))
                self._state_db.record_notification_attempt(ins.pending_review_id, delivered)
            return True

        if decision == "review_required" and result.match_outcome == "zero_match":
            raw_handle = None
            summary.discarded += 1
            return True

        if decision == "review_required" and result.match_outcome == "multiple_match":
            raw_handle = None
            status = (
                STATUS_DUPLICATE_RESOLUTION_REQUIRED
                if result.candidate_collision
                else STATUS_PENDING_REVIEW
            )
            ins = self._state_db.insert_pending_review(
                source=source, source_event_id=source_event_id, decision=decision,
                match_outcome=result.match_outcome, occurred_at=_iso(event_ts),
                duration_s=duration_s, direction=direction, answered=answered,
                status=status,
            )
            summary.pending_review += 1
            if ins.genuine_insert and not self._defer_notifications:
                row = self._state_db.get_pending_review(ins.pending_review_id)
                if result.candidate_collision:
                    payload = build_collision_payload(row)
                else:
                    payload = build_multiple_match_payload(row, result.candidate_rows or [])
                delivered = self._notifier.send(payload)
                self._state_db.record_notification_attempt(ins.pending_review_id, delivered)
            return True

        if decision in ("deny_paused", "deny_retired"):
            raw_handle = None
            outcome = OUTCOME_DENY_PAUSED if decision == "deny_paused" else OUTCOME_DENY_RETIRED
            self._state_db.insert_processed_outcome(source, source_event_id, outcome)
            summary.discarded += 1
            return True

        # Unexpected decision. No open-ended text is persisted.
        raw_handle = None
        self._state_db.insert_processed_outcome(source, source_event_id, OUTCOME_UNEXPECTED_DECISION)
        summary.discarded += 1
        return True

    def _mirror_note_for_admission(
        self,
        source: str,
        ledger_result: Any,
        contact_id: str | None,
        event_ts: datetime,
        duration_s: int | None,
        direction: str | None,
        answered: int | None,
        summary: RunSummary,
    ) -> bool:
        """Mirror one newly admitted/override-admitted event to a GHL contact
        note. Desk-only -- Plaud stays excluded from the mirror the same way
        it stays excluded from ingestion, pending OPS-110 -- and a no-op when
        no note_mirror was wired in (every pre-existing caller/test). Returns
        False (holding the cursor at this record) iff the mirror did not
        reach a marker-verified state; the failure is also counted so it is
        visible in the run summary."""
        if source != SOURCE_DESK_CALL or self._note_mirror is None:
            return True
        outcome = self._note_mirror.mirror_event(
            event_id=ledger_result.event_id,
            contact_id=contact_id,
            occurred_at=event_ts,
            direction=direction,
            answered=answered,
            duration_s=duration_s,
            trigger=f"OPS-18 desk_call note mirror for ledger event {ledger_result.event_id}",
        )
        if outcome.outcome not in _NOTE_MIRROR_OK_OUTCOMES:
            summary.note_errors += 1
            summary.errors += 1
            return False
        if outcome.outcome == "created":
            summary.notes_created += 1
        elif outcome.outcome == "recovered":
            summary.notes_recovered += 1
        if outcome.outcome in ("created", "recovered"):
            summary.note_references.append(
                {
                    "note_id": outcome.note_id,
                    "contact_id": contact_id,
                    "contact_url": contact_detail_url(TEAM_DUNCAN_LOCATION_ID, contact_id),
                }
            )
        return True

    # --- Notification retry (manual-run only; no background process) ----------

    def _process_due_retries(self) -> None:
        for row in self._state_db.due_for_notification_retry():
            if row.source not in self._enabled_sources:
                continue
            payload = self._rebuild_retry_payload(row)
            if payload is None:
                continue
            delivered = self._notifier.send(payload)
            self._state_db.record_notification_attempt(row.id, delivered)

    def _rebuild_retry_payload(self, row: PendingReviewRow) -> dict[str, Any] | None:
        if row.decision == "deny_pre_activation":
            return build_deny_pre_activation_payload(row)
        if row.match_outcome == "zero_match" and row.source == SOURCE_PLAUD:
            return build_plaud_zero_match_payload(row, None)
        if row.match_outcome == "multiple_match":
            try:
                raw_handle = self._sealed_refetch_raw_handle(row)
            except (SourceMissingError, SourceAmbiguousError):
                self._fail_source_refetch(row)
                return None
            if raw_handle is None:
                return None
            fresh = self._registry.resolve_event(
                raw_handle, datetime.fromisoformat(row.occurred_at),
                source=row.source, source_event_id=row.source_event_id,
            )
            raw_handle = None
            if fresh.candidate_collision:
                self._state_db.advance_pending_review_stage(
                    row.id, STATUS_DUPLICATE_RESOLUTION_REQUIRED, actor="system",
                    detail="collision_discovered_on_retry",
                )
                return build_collision_payload(row)
            return build_multiple_match_payload(row, fresh.candidate_rows or [])
        return None

    # --- Sealed exact-source re-fetch (selection / creation / activation / replay) --

    def _sealed_refetch_raw_handle(self, row: PendingReviewRow) -> str | None:
        """Re-fetches the exact source event's raw handle. Raises
        SourceMissingError/SourceAmbiguousError -- never returns a guess."""
        if row.source == SOURCE_PLAUD:
            record = self._plaud.fetch_by_identity(row.source_event_id)
            if record is None:
                raise SourceMissingError(row.id)
            return record.caller_handle
        # SOURCE_DESK_CALL: point lookup needs (zdate, zoriginated, zanswered,
        # duration). We don't persist zdate/zoriginated directly, but the
        # collector's point lookup is keyed by occurred_at (-> zdate) and the
        # non-PII coordinates already on the pending_review row.
        from .collectors.call_history_collector import utc_to_apple_epoch

        target_zdate = utc_to_apple_epoch(datetime.fromisoformat(row.occurred_at))
        zoriginated = 1 if row.direction == "outbound" else 0
        zanswered = row.answered if row.answered is not None else 0
        try:
            record = self._desk.fetch_exact_event(
                target_zdate=target_zdate, zoriginated=zoriginated,
                zanswered=zanswered, duration_s=row.duration_s,
                expected_source_event_id=row.source_event_id,
            )
        except AmbiguousReplayError:
            raise SourceAmbiguousError(row.id) from None
        if record is None:
            raise SourceMissingError(row.id)
        return record.raw_handle

    def _fail_source_refetch(self, row: PendingReviewRow) -> None:
        try:
            self._sealed_refetch_raw_handle(row)
        except SourceMissingError:
            self._state_db.advance_pending_review_stage(row.id, STATUS_SOURCE_MISSING, actor="system")
        except SourceAmbiguousError:
            self._state_db.advance_pending_review_stage(row.id, STATUS_SOURCE_AMBIGUOUS, actor="system")

    # --- Selection (multiple_match): Clay's exact opaque choice ----------------

    def resolve_pending_selection(self, pending_review_id: str, selection_token: str) -> PendingReviewRow:
        """Consume Clay's exact candidate selection. Re-fetches the raw handle
        and re-resolves fresh (tokens are re-derived, never stored), so a
        collision that appears only now still fails closed. Never guesses."""
        row = self._require_row(pending_review_id)
        if row.status != STATUS_PENDING_REVIEW or row.match_outcome != "multiple_match":
            raise ActionNotApprovedError(
                f"pending_review {pending_review_id!r} is not awaiting a candidate selection."
            )

        try:
            raw_handle = self._sealed_refetch_raw_handle(row)
        except SourceMissingError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_MISSING, actor="clay"
            )
        except SourceAmbiguousError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_AMBIGUOUS, actor="clay"
            )

        fresh = self._registry.resolve_event(
            raw_handle, datetime.fromisoformat(row.occurred_at),
            source=row.source, source_event_id=row.source_event_id,
            selection_token=selection_token,
        )
        raw_handle = None

        if fresh.match_outcome != "multiple_match" or fresh.candidate_collision:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_DUPLICATE_RESOLUTION_REQUIRED, actor="clay",
                detail="collision_discovered_at_selection",
            )

        if fresh.selected_contact_id is None:
            raise ActionNotApprovedError("Selection token did not match any current candidate.")

        return self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_CONTACT_SELECTED, actor="clay",
            resolved_contact_id=fresh.selected_contact_id,
        )

    # --- Contact creation (zero_match): sealed re-fetch, read-before-create ----

    def with_sealed_creation_handle(
        self,
        pending_review_id: str,
        token: str,
        perform_lookup_and_create: Callable[[str], dict[str, Any]],
    ) -> PendingReviewRow:
        """Consumes a create_contact action-approval token, re-fetches the
        exact source event's raw handle, and calls *perform_lookup_and_create*
        with it synchronously. The callback (OPS-18-supplied) performs the
        read-before-create lookup and, if needed, create_contact via the
        OPS-104 scoped client. The raw handle exists only as a local
        variable and this call's argument; cleared in `finally` regardless
        of outcome."""
        row = self._require_row(pending_review_id)
        if row.status != STATUS_PENDING_REVIEW or row.match_outcome != "zero_match":
            raise ActionNotApprovedError(
                f"pending_review {pending_review_id!r} is not awaiting contact creation."
            )
        if not self._state_db.consume_action_approval_token(token, pending_review_id, ACTION_CREATE_CONTACT):
            raise ActionNotApprovedError(
                f"No valid create_contact approval token for pending_review {pending_review_id!r}."
            )

        idem_key = creation_idempotency_key(row.source, row.source_event_id)
        self._state_db.set_idempotency_key(pending_review_id, idem_key)
        row = self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_CONTACT_CREATION_PENDING, actor="clay"
        )

        raw_handle = None
        try:
            raw_handle = self._sealed_refetch_raw_handle(row)
        except SourceMissingError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_MISSING, actor="clay"
            )
        except SourceAmbiguousError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_AMBIGUOUS, actor="clay"
            )

        try:
            created = perform_lookup_and_create(raw_handle)
        finally:
            raw_handle = None

        contact_id = created.get("id") if isinstance(created, dict) else None
        return self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_CONTACT_CREATED, actor="clay",
            resolved_contact_id=contact_id,
        )

    # --- Activation (new contact only): separate prepare + confirm -------------

    def prepare_new_contact_activation(self, pending_review_id: str, ghl_reader: Any) -> Any:
        row = self._require_row(pending_review_id)
        if row.status != STATUS_CONTACT_CREATED or not row.resolved_contact_id:
            raise ActionNotApprovedError(
                f"pending_review {pending_review_id!r} has no created contact awaiting activation."
            )
        prepare_result = self._registry.prepare_activation(row.resolved_contact_id, ghl_reader)
        if prepare_result.status == "ready_for_confirmation":
            self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_AWAITING_ACTIVATION_CONFIRMATION, actor="clay"
            )
        return prepare_result

    def confirm_new_contact_activation(self, pending_review_id: str, activation_token: str) -> Any:
        row = self._require_row(pending_review_id)
        if row.status != STATUS_AWAITING_ACTIVATION_CONFIRMATION:
            raise ActionNotApprovedError(
                f"pending_review {pending_review_id!r} is not awaiting activation confirmation."
            )
        confirm_result = self._registry.confirm_activation(activation_token)
        if confirm_result.status in ("activated", "already_activated"):
            self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_CONTACT_ACTIVATED, actor="clay"
            )
        return confirm_result

    # --- Grant + replay: exact contact, Team Duncan location, source, event ----

    def grant_and_replay(
        self, pending_review_id: str, token: str, contact_id: str, *, approval_reason: str
    ) -> PendingReviewRow:
        """Consumes a grant_override approval token, re-fetches the exact
        source event once more (sealed), grants an event-scoped override for
        *contact_id*, then replays admission through the existing ledger.

        Known boundary: the ledger's own `record_event`/`resolve_event`
        (activity_ledger.py, not modified by this build) always re-resolves
        the raw handle itself and only ever admits to whichever contact that
        handle's HMAC uniquely matches -- an override grant bypasses the
        *activation cutoff* for that unique match, it does not (and
        structurally cannot, without editing activity_ledger.py) redirect
        admission to a different contact. For a STATUS_CONTACT_SELECTED row
        reached via a genuinely shared handle (two+ registered contacts on
        the same phone/email), the handle still resolves as multiple_match
        here too, so the ledger discards it. This method never pretends
        otherwise: it fails closed to STATUS_FAILED with the ledger's own
        decision recorded, rather than reporting a false admission."""
        row = self._require_row(pending_review_id)
        eligible = (
            (row.status == STATUS_PENDING_REVIEW and row.decision == "deny_pre_activation")
            or row.status == STATUS_CONTACT_SELECTED
            or row.status == STATUS_CONTACT_ACTIVATED
        )
        if not eligible:
            raise ActionNotApprovedError(
                f"pending_review {pending_review_id!r} is not awaiting a grant."
            )
        if not self._state_db.consume_action_approval_token(token, pending_review_id, ACTION_GRANT_OVERRIDE):
            raise ActionNotApprovedError(
                f"No valid grant_override approval token for pending_review {pending_review_id!r}."
            )

        try:
            raw_handle = self._sealed_refetch_raw_handle(row)
        except SourceMissingError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_MISSING, actor="clay"
            )
        except SourceAmbiguousError:
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_SOURCE_AMBIGUOUS, actor="clay"
            )

        self._activity_ledger.grant_override(
            contact_id, TEAM_DUNCAN_LOCATION_ID, row.source, row.source_event_id, approval_reason,
        )
        row = self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_GRANT_ISSUED, actor="clay", resolved_contact_id=contact_id,
        )

        event_ts = datetime.fromisoformat(row.occurred_at)
        provenance = {"duration_s": row.duration_s, "direction": row.direction, "answered": row.answered}
        ledger_result = self._activity_ledger.record_event(
            row.source, row.source_event_id, raw_handle, event_ts, provenance
        )
        raw_handle = None

        if ledger_result.outcome != "override_admitted":
            return self._state_db.advance_pending_review_stage(
                pending_review_id, STATUS_FAILED, actor="system",
                failure_stage="replay", failure_detail=ledger_result.decision or "not_admitted",
            )

        row = self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_REPLAYED, actor="clay"
        )

        if row.source == SOURCE_DESK_CALL and self._note_mirror is not None:
            outcome = self._note_mirror.mirror_event(
                event_id=ledger_result.event_id,
                contact_id=contact_id,
                occurred_at=event_ts,
                direction=row.direction,
                answered=row.answered,
                duration_s=row.duration_s,
                trigger=f"OPS-18 desk_call note mirror for ledger event {ledger_result.event_id}",
            )
            if outcome.outcome not in _NOTE_MIRROR_OK_OUTCOMES:
                return self._state_db.advance_pending_review_stage(
                    pending_review_id, STATUS_FAILED, actor="system",
                    failure_stage="note_mirror", failure_detail=outcome.outcome,
                )

        return self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_COMPLETE, actor="clay"
        )

    # --- Dismiss ----------------------------------------------------------------

    def dismiss_pending_review(self, pending_review_id: str, reason: str) -> PendingReviewRow:
        return self._state_db.advance_pending_review_stage(
            pending_review_id, STATUS_DISMISSED, actor="clay", detail=reason
        )

    def _require_row(self, pending_review_id: str) -> PendingReviewRow:
        row = self._state_db.get_pending_review(pending_review_id)
        if row is None:
            raise ValueError(f"pending_review {pending_review_id!r} not found.")
        return row


def _iso(dt: datetime) -> str:
    return dt.isoformat()
