"""OPS-18 audited GHL contact-note mirror for admitted Desk call events.

For every newly admitted or override-admitted Desk call event, exactly one
GoHighLevel contact note is created on the exact resolved Team Duncan
contact. The external activity ledger (activity_ledger.py) remains the
source of truth; this module is an idempotent mirror onto it, never an
alternate admission path.

Every GHL write here goes through the audited GoHighLevelWriteClient
(tools.ghl_client) -- create_note/get_note -- never a direct unlogged REST
call.

Dedupe identity lives entirely in local durable state: the note_mirror
table (event_id -> note_id/content_hash/status, see ingestion_state_db.py)
plus the write-audit trigger, never in the visible note body. This module
writes no event marker, hidden field, zero-width character, HTML comment,
or encoded identifier into a note -- GHL's notes API exposes no supported
hidden metadata field, and this module never pretends otherwise by hiding
one in visible text. Idempotency on retry is anchored purely on the local
note_mirror row: a ``complete`` row's note id is read back and verified
before this module ever reports "no write needed" and skips create.

Unavoidable recovery limitation: because there is nothing to scan for in a
note body, if the local note_mirror row (and every backup of it) is lost
between a successful create_note POST landing on GHL and the local state
write that would have recorded it, this module cannot discover the
already-created note again. The next attempt for that same event_id
creates a second note. This is a structural limit of GHL's note API, not a
bug here -- recoverability depends entirely on the durability of local
state, so that state (and its backups) must be protected accordingly.

Note bodies carry exactly one plain line: direction, answered/missed
status, and duration -- the only call metadata Clay has authorized for
visible note text. Never a raw phone number, email, Apple handle,
transcript, summary, occurrence date, source label, or database
identifier.

Every phone-call note this module creates uses the fixed light-green
CALL_NOTE_COLOR -- Clay requires all call notes, mirrored or pre-existing,
to be visually distinct by that one color.

OPS-75 iMessage lane (mirror_message_event): unlike the call lane above,
this lane writes a deterministic, visible marker
(``imessage_note_marker``) into the note body, derived only from the
ledger's own non-reversible event_id -- never from a raw handle. This is a
narrow, additive exception scoped only to mirror_message_event: it exists
so a lost local note_mirror row can still be recovered by searching GHL
note bodies for the marker (see mirror_message_event's pre-POST marker
search) before ever falling back to create -- something the call lane
above still cannot do, and does not attempt to.

Clay approved the message text for the GHL note body only, never for the
local write-audit log. mirror_message_event's create_note call therefore
opts into create_note(..., redact_body_in_audit=True): the audit outcome's
after["body"] is replaced with a fixed marker before it is recorded, while
the real POST body and the read-back verified above keep the exact text.
mirror_event (the call lane) never sets this and is unaffected.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

#: GHL's app UI has no note-specific deep link; only the contact detail page
#: is addressable. Building a note-level URL here would claim a capability
#: GHL does not support, so callers only ever get this contact-level link.
GHL_APP_BASE_URL = "https://app.gohighlevel.com"

#: Clay requires every phone-call note (mirrored and pre-existing) to use
#: this fixed light-green GHL note color. This is the only color this
#: module ever writes.
CALL_NOTE_COLOR = "#D9EAD3"

_DIRECTION_LABELS = {"inbound": "Incoming", "outbound": "Outgoing"}

#: iMessage lane: title/color pair depends only on direction (is_from_me).
IMESSAGE_SENT_TITLE = "iMessage · Sent"
IMESSAGE_RECEIVED_TITLE = "iMessage · Received"
IMESSAGE_SENT_COLOR = "#007AFF"
IMESSAGE_RECEIVED_COLOR = "#8E8E93"

#: Outcomes that mean "no further action needed this run" -- a note exists
#: (or now does) for this event. "recovered" is iMessage-lane only: the
#: call lane's mirror_event() never produces it (see module docstring).
OK_OUTCOMES = frozenset({"created", "already_complete", "recovered"})


def contact_detail_url(location_id: str, contact_id: str) -> str:
    """A stable GHL contact-detail URL -- the only supported deep link."""
    return f"{GHL_APP_BASE_URL}/v2/location/{location_id}/contacts/detail/{contact_id}"


def _format_duration(duration_s: int | None) -> str:
    """Normalize *duration_s* to a non-negative whole number of seconds
    (the existing source semantics: clamp negative to zero, truncate any
    fractional value) and render it as one plain phrase: '0 sec', 'N sec'
    under a minute, 'N min' on an exact minute, or 'N min M sec' otherwise.
    """
    if duration_s is None:
        return "unknown"
    total = max(0, int(duration_s))
    minutes, seconds = divmod(total, 60)
    if minutes and seconds:
        return f"{minutes} min {seconds} sec"
    if minutes:
        return f"{minutes} min"
    return f"{seconds} sec"


def format_note_body(
    *,
    direction: str | None,
    answered: int | None,
    duration_s: int | None,
) -> str:
    """Exactly one plain line: '<Incoming|Outgoing> call · <Answered|Missed>
    · <duration>'. No event marker, date, source label, or field labels --
    Clay requires the visible note text to read as a single clean line."""
    direction_label = _DIRECTION_LABELS.get(direction, "Unknown direction")
    status_label = "Unknown status" if answered is None else ("Answered" if answered else "Missed")
    return f"{direction_label} call · {status_label} · {_format_duration(duration_s)}"


def imessage_note_marker(event_id: str) -> str:
    """A deterministic, visible marker for one ledger event_id, derived only
    from the ledger's own non-reversible event_id (sha256(source|
    source_event_id) -- see activity_ledger._event_id) -- never from a raw
    handle. iMessage lane only; see module docstring."""
    return f"imsg-evt:{event_id[:16]}"


def format_imessage_note_body(*, text: str, occurred_at: datetime, marker: str) -> str:
    """'{text}\\n\\n{compact_ts} · {marker}'. *text* is the exact original
    message text, written verbatim (only a leading/trailing whitespace
    strip -- no truncation, no escaping, no HTML), per the binding
    contract for this lane. *compact_ts* is *occurred_at* in UTC formatted
    '%Y-%m-%d %H:%M UTC'."""
    if occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(timezone.utc)
    compact_ts = occurred_at.strftime("%Y-%m-%d %H:%M UTC")
    return f"{text.strip()}\n\n{compact_ts} · {marker}"


@dataclass
class MirrorResult:
    outcome: str  # 'created' | 'already_complete' | 'recovered' | 'error'
    note_id: str | None = None


class NoteMirror:
    """Crash-safe, idempotent GHL contact-note mirror for one ledger event.

    *ghl_client* is a scoped GoHighLevelWriteClient (create_note/get_note);
    *state_db* is the OPS-18 IngestionStateDb's note_mirror table. This
    class only decides whether a write is needed and verifies it landed --
    it never guesses at admission and is never called for a non-admitted
    event.

    Dedupe for mirror_event (the call lane) is anchored entirely on the
    local note_mirror row keyed by event_id -- that lane never scans GHL
    note bodies for a marker (it writes none), so there is nothing there to
    scan for; see the module docstring for the resulting recovery
    limitation if local state is lost. mirror_message_event (the iMessage
    lane) is the one exception: it writes a marker and scans for it, so a
    lost local row is still recoverable there.
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
            return self._verify_existing(existing.note_id, contact_id)

        body = format_note_body(direction=direction, answered=answered, duration_s=duration_s)
        return self._create_and_verify(
            event_id=event_id, contact_id=contact_id, body=body, trigger=trigger,
            color=CALL_NOTE_COLOR,
        )

    def mirror_message_event(
        self,
        *,
        event_id: str,
        contact_id: str,
        text: str,
        occurred_at: datetime,
        is_from_me: bool,
        trigger: str,
    ) -> MirrorResult:
        """OPS-75 iMessage lane. See module docstring for how this differs
        from mirror_event: a visible marker is written into the body so a
        lost local note_mirror row can still be recovered by searching GHL
        before this ever falls back to create."""
        existing = self._state_db.get_note_mirror(event_id)
        if existing is not None and existing.status == "complete" and existing.note_id:
            return self._verify_existing(existing.note_id, contact_id)

        marker = imessage_note_marker(event_id)
        recovered = self._recover_by_marker(event_id, contact_id, marker)
        if recovered is not None:
            return recovered

        title = IMESSAGE_SENT_TITLE if is_from_me else IMESSAGE_RECEIVED_TITLE
        color = IMESSAGE_SENT_COLOR if is_from_me else IMESSAGE_RECEIVED_COLOR
        body = format_imessage_note_body(text=text, occurred_at=occurred_at, marker=marker)
        return self._create_and_verify(
            event_id=event_id, contact_id=contact_id, body=body, trigger=trigger,
            color=color, title=title, redact_body_in_audit=True,
        )

    def _recover_by_marker(
        self, event_id: str, contact_id: str, marker: str
    ) -> MirrorResult | None:
        """Scan existing GHL notes for *marker* before ever creating a new
        one. Returns ``None`` (proceed to create) iff no matching note was
        found; otherwise a terminal MirrorResult ('recovered' or 'error')."""
        try:
            notes = self._ghl.list_notes(contact_id)
        except Exception as exc:
            log.error("note_mirror: marker search failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error")

        match_id = None
        for note in notes:
            note_body = note.get("body") if isinstance(note, dict) else None
            if isinstance(note_body, str) and marker in note_body:
                match_id = note.get("id")
                break
        if not match_id:
            return None

        try:
            readback = self._ghl.get_note(contact_id, match_id)
        except Exception as exc:
            log.error("note_mirror: marker-matched read-back failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error", note_id=match_id)
        if not isinstance(readback, dict) or not readback.get("id"):
            log.error("note_mirror: marker-matched note no longer resolves")
            return MirrorResult(outcome="error", note_id=match_id)

        readback_body = readback.get("body")
        content_hash = hashlib.sha256(
            (readback_body if isinstance(readback_body, str) else "").encode("utf-8")
        ).hexdigest()
        self._state_db.mark_note_mirror_complete(
            event_id, contact_id=contact_id, note_id=match_id, content_hash=content_hash,
        )
        return MirrorResult(outcome="recovered", note_id=match_id)

    def _create_and_verify(
        self,
        *,
        event_id: str,
        contact_id: str,
        body: str,
        trigger: str,
        color: str | None = None,
        title: str | None = None,
        redact_body_in_audit: bool = False,
    ) -> MirrorResult:
        """Shared create+verify+record steps for both mirror_event and
        mirror_message_event: record the attempt, create_note, read back,
        verify the body matches exactly, then mark complete. Kept as one
        code path so the call lane's behavior is provably unchanged by this
        build rather than duplicated and left free to drift.

        *redact_body_in_audit* is passed through to create_note() only when
        true, so mirror_event's (the call lane's) create_note call is issued
        with the exact same keyword set as before this build -- see
        create_note()'s docstring in tools.ghl_client for the redaction
        contract itself."""
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self._state_db.record_note_mirror_attempt(
            event_id, contact_id=contact_id, content_hash=content_hash
        )

        create_kwargs: dict[str, Any] = {"trigger": trigger, "color": color}
        if title is not None:
            create_kwargs["title"] = title
        if redact_body_in_audit:
            create_kwargs["redact_body_in_audit"] = True
        try:
            created = self._ghl.create_note(contact_id, body, **create_kwargs)
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
        if readback_body != body:
            log.error("note_mirror: read-back body did not match what was written")
            return MirrorResult(outcome="error", note_id=note_id)

        self._state_db.mark_note_mirror_complete(
            event_id, contact_id=contact_id, note_id=note_id, content_hash=content_hash,
        )
        return MirrorResult(outcome="created", note_id=note_id)

    def _verify_existing(self, note_id: str, contact_id: str) -> MirrorResult:
        """A local row already claims this event's note was created. Read
        it back rather than trusting local state blindly before reporting
        "no write needed" and skipping create."""
        try:
            readback = self._ghl.get_note(contact_id, note_id)
        except Exception as exc:
            log.error("note_mirror: verify read of existing note failed [%s]", type(exc).__name__)
            return MirrorResult(outcome="error", note_id=note_id)
        if not isinstance(readback, dict) or not readback.get("id"):
            log.error("note_mirror: existing complete note no longer resolves")
            return MirrorResult(outcome="error", note_id=note_id)
        return MirrorResult(outcome="already_complete", note_id=note_id)


__all__ = [
    "CALL_NOTE_COLOR",
    "GHL_APP_BASE_URL",
    "IMESSAGE_RECEIVED_COLOR",
    "IMESSAGE_RECEIVED_TITLE",
    "IMESSAGE_SENT_COLOR",
    "IMESSAGE_SENT_TITLE",
    "OK_OUTCOMES",
    "MirrorResult",
    "NoteMirror",
    "contact_detail_url",
    "format_imessage_note_body",
    "format_note_body",
    "imessage_note_marker",
]
