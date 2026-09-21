"""OPS-18 audited GHL contact-note mirror for admitted Desk call events.

For every newly admitted or override-admitted Desk call event, exactly one
GoHighLevel contact note is created on the exact resolved Team Duncan
contact. The external activity ledger (activity_ledger.py) remains the
source of truth; this module is an idempotent mirror onto it, never an
alternate admission path.

Every GHL write here goes through the audited GoHighLevelWriteClient
(tools.ghl_client) -- create_note/list_notes/get_note -- never a direct
unlogged REST call. Crash-safe idempotency is anchored on a deterministic,
non-PII marker derived from the ledger's own event_id and embedded in the
note body: before every write this module searches the contact's existing
notes for that marker (so a POST that already reached GHL, but whose local
state write then failed, is discovered and never duplicated), and after a
write it requires the note's own id plus an exact GET read-back whose body
contains the marker before local state is ever marked complete.

Note bodies carry only decision-useful call metadata already authorized for
the ledger: occurrence date/time (America/Chicago, with timezone),
incoming/outgoing direction, answered/missed status, duration, a fixed
source label, and the marker. Never a raw phone number, email, Apple
handle, transcript, summary, or source database identifier.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_CHICAGO_TZ = ZoneInfo("America/Chicago")

#: Prefix for the deterministic, non-PII event marker embedded in every
#: mirrored note body. Derived from the ledger's own event_id (already a
#: non-reversible sha256 digest) -- never from a raw handle.
_MARKER_PREFIX = "ops18-event"

#: GHL's app UI has no note-specific deep link; only the contact detail page
#: is addressable. Building a note-level URL here would claim a capability
#: GHL does not support, so callers only ever get this contact-level link.
GHL_APP_BASE_URL = "https://app.gohighlevel.com"

_DIRECTION_LABELS = {"inbound": "Incoming", "outbound": "Outgoing"}

#: Outcomes that mean "no further action needed this run" -- a note exists
#: (or now does) bearing this event's marker.
OK_OUTCOMES = frozenset({"created", "recovered", "already_complete"})


def note_marker_for_event(event_id: str) -> str:
    """Deterministic, non-PII marker for *event_id*, embedded verbatim in the
    note body and used to find a previously-created note on retry/recovery."""
    return f"[{_MARKER_PREFIX}:{event_id}]"


def contact_detail_url(location_id: str, contact_id: str) -> str:
    """A stable GHL contact-detail URL -- the only supported deep link."""
    return f"{GHL_APP_BASE_URL}/v2/location/{location_id}/contacts/detail/{contact_id}"


def _format_duration(duration_s: int | None) -> str:
    if duration_s is None:
        return "unknown"
    total = max(0, int(duration_s))
    minutes, seconds = divmod(total, 60)
    if minutes:
        return f"{minutes}m {seconds}s ({total}s)"
    return f"{seconds}s"


def format_note_body(
    *,
    occurred_at: datetime,
    direction: str | None,
    answered: int | None,
    duration_s: int | None,
    marker: str,
    source_label: str = "Desk",
) -> str:
    """Decision-useful call metadata only, plus the marker. Never a raw
    handle, transcript, summary, or source database identifier."""
    local = occurred_at.astimezone(_CHICAGO_TZ)
    when = local.strftime("%Y-%m-%d %I:%M %p %Z")
    direction_label = _DIRECTION_LABELS.get(direction, "Unknown direction")
    status_label = "Unknown status" if answered is None else ("Answered" if answered else "Missed")
    lines = [
        f"{source_label} call - {when}",
        f"Direction: {direction_label}",
        f"Status: {status_label}",
        f"Duration: {_format_duration(duration_s)}",
        marker,
    ]
    return "\n".join(lines)


@dataclass
class MirrorResult:
    outcome: str  # 'created' | 'recovered' | 'already_complete' | 'error'
    note_id: str | None = None


class NoteMirror:
    """Crash-safe, idempotent GHL contact-note mirror for one ledger event.

    *ghl_client* is a scoped GoHighLevelWriteClient (create_note/list_notes/
    get_note); *state_db* is the OPS-18 IngestionStateDb's note_mirror table.
    This class only decides whether a write is needed and verifies it
    landed -- it never guesses at admission and is never called for a
    non-admitted event.
    """

    def __init__(self, ghl_client: Any, state_db: Any) -> None:
        self._ghl = ghl_client
        self._state_db = state_db

    def mirror_event(
        self,
        *,
        event_id: str,
        contact_id: str,
        occurred_at: datetime,
        direction: str | None,
        answered: int | None,
        duration_s: int | None,
        trigger: str,
        source_label: str = "Desk",
    ) -> MirrorResult:
        existing = self._state_db.get_note_mirror(event_id)
        if existing is not None and existing.status == "complete" and existing.note_id:
            return MirrorResult(outcome="already_complete", note_id=existing.note_id)

        marker = note_marker_for_event(event_id)
        body = format_note_body(
            occurred_at=occurred_at, direction=direction, answered=answered,
            duration_s=duration_s, marker=marker, source_label=source_label,
        )
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self._state_db.record_note_mirror_attempt(
            event_id, contact_id=contact_id, content_hash=content_hash
        )

        try:
            found_note_id = self._find_existing_marker_note(contact_id, marker)
        except Exception as exc:
            log.error("note_mirror: existing-note search failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error")
        if found_note_id is not None:
            self._state_db.mark_note_mirror_complete(
                event_id, contact_id=contact_id, note_id=found_note_id,
                content_hash=content_hash,
            )
            return MirrorResult(outcome="recovered", note_id=found_note_id)

        try:
            created = self._ghl.create_note(contact_id, body, trigger=trigger)
        except Exception as exc:
            log.error("note_mirror: create_note failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error")

        note_id = created.get("id") if isinstance(created, dict) else None
        if not note_id:
            log.error("note_mirror: create_note returned no note id")
            return MirrorResult(outcome="error")

        try:
            readback = self._ghl.get_note(contact_id, note_id)
        except Exception as exc:
            log.error("note_mirror: read-back failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error", note_id=note_id)

        readback_body = readback.get("body") if isinstance(readback, dict) else None
        if not isinstance(readback_body, str) or marker not in readback_body:
            log.error("note_mirror: read-back did not contain the expected marker")
            return MirrorResult(outcome="error", note_id=note_id)

        self._state_db.mark_note_mirror_complete(
            event_id, contact_id=contact_id, note_id=note_id, content_hash=content_hash,
        )
        return MirrorResult(outcome="created", note_id=note_id)

    def _find_existing_marker_note(self, contact_id: str, marker: str) -> str | None:
        for note in self._ghl.list_notes(contact_id):
            if not isinstance(note, dict):
                continue
            existing_body = note.get("body")
            if isinstance(existing_body, str) and marker in existing_body:
                return note.get("id")
        return None


__all__ = [
    "GHL_APP_BASE_URL",
    "OK_OUTCOMES",
    "MirrorResult",
    "NoteMirror",
    "contact_detail_url",
    "format_note_body",
    "note_marker_for_event",
]
