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
one in visible text, and never scans note bodies to recover from lost
local state. Idempotency on retry is anchored purely on the local
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

That said, this is not the retry path an ordinary collector re-run takes.
The primary retry barrier is one level upstream of this module: a
newly-admitted ledger event is mirrored only when
``ledger_result.genuine_insert`` is true (see collectors/
imessage_collector.py), and ``genuine_insert`` reflects the activity
ledger's own ``UNIQUE(source, source_event_id)`` insert -- which already
landed durably before this module's create_note POST was even attempted.
So a successful GHL POST followed by an interruption before the
note_mirror write completes still cannot create a second note on an
ordinary collector retry: the ledger row already exists, the retry's
insert is a no-op (``genuine_insert=False``), and mirror_message_event is
never called for it. Only the simultaneous loss of *all* local state --
both the ledger's row and the note_mirror row (and their backups) -- loses
this dedupe/provenance guarantee; a lone lost note_mirror row with the
ledger row intact never causes a second note on retry, since retry never
reaches this module for that event again.

Note bodies carry exactly one plain line: direction, answered/missed
status, and duration -- the only call metadata Clay has authorized for
visible note text. Never a raw phone number, email, Apple handle,
transcript, summary, occurrence date, source label, or database
identifier.

Every phone-call note this module creates uses the fixed light-green
CALL_NOTE_COLOR -- Clay requires all call notes, mirrored or pre-existing,
to be visually distinct by that one color.

OPS-75 iMessage lane (mirror_message_event): the note body is the exact
source message text and nothing else -- no marker, no timestamp, no
source label (see format_imessage_note_body). The source message's own
timestamp instead drives the note *title* (format_imessage_note_title),
converted to America/Chicago with the stdlib zoneinfo module so the
printed time is correct across both CDT and CST. Sent/Received each use a
fixed, exact GHL native color (see IMESSAGE_SENT_COLOR/
IMESSAGE_RECEIVED_COLOR below) instead of the call lane's single green.

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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

#: GHL's app UI has no note-specific deep link; only the contact detail page
#: is addressable. Building a note-level URL here would claim a capability
#: GHL does not support, so callers only ever get this contact-level link.
GHL_APP_BASE_URL = "https://app.gohighlevel.com"

#: Clay requires every phone-call note (mirrored and pre-existing) to use
#: this fixed light-green GHL note color. This is the only color this
#: module ever writes for the call lane.
CALL_NOTE_COLOR = "#D9EAD3"

_DIRECTION_LABELS = {"inbound": "Incoming", "outbound": "Outgoing"}

#: iMessage lane: exact live GHL native colors, sourced from Clay's own
#: hand-edited Note (blue-100, sent) and GHL's published Storybook color
#: palette (gray-100, received) -- never a substitute gray/blue.
IMESSAGE_SENT_COLOR = "#d1e9ff"
IMESSAGE_RECEIVED_COLOR = "#f2f4f7"

#: The source message timestamp is always rendered in this fixed zone,
#: regardless of host locale/timezone, so DST is correct year-round.
_IMESSAGE_TITLE_TZ = ZoneInfo("America/Chicago")

#: Fixed English month abbreviations -- never strftime's locale-dependent
#: %b, so the title text never depends on the host's configured locale.
_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

#: Outcomes that mean "no further action needed this run" -- a note exists
#: (or now does) for this event. "recovered" is retained only for backward
#: compatibility with existing callers (the collector, ingestion_runner)
#: that still check for it; this module never produces it any more -- there
#: is no marker to recover by (see module docstring), so every call either
#: takes the local-state fast path, creates, or errors.
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


def format_imessage_note_body(*, text: str) -> str:
    """The note body is exactly the source iMessage text, written verbatim
    -- only a leading/trailing whitespace strip (no truncation, no
    escaping, no HTML). No timestamp, marker, source label, or any other
    metadata; see the module docstring for the full contract."""
    return text.strip()


def format_imessage_note_title(*, occurred_at: datetime, is_from_me: bool) -> str:
    """'iMessage · <Sent|Received> · <Mon> <D> <YYYY>, <H>:<MM> <AM|PM>'.

    *occurred_at* is the source message's own timestamp (never Note
    creation time) and must be timezone-aware; it is converted to
    America/Chicago with :mod:`zoneinfo` so the result is correct across
    both CDT and CST. Month is an abbreviated English name, day has no
    leading zero, hour is 12-hour without a leading zero, minutes are
    always two digits, and AM/PM is uppercase -- all formatted by hand
    (never strftime's locale-dependent %b/%I/%p) so the output never
    depends on the host's locale.
    """
    if occurred_at.tzinfo is None:
        raise ValueError(
            "format_imessage_note_title() requires a timezone-aware occurred_at."
        )
    local = occurred_at.astimezone(_IMESSAGE_TITLE_TZ)
    hour12 = local.hour % 12 or 12
    period = "AM" if local.hour < 12 else "PM"
    direction_label = "Sent" if is_from_me else "Received"
    month_abbr = _MONTH_ABBREVIATIONS[local.month - 1]
    return (
        f"iMessage · {direction_label} · {month_abbr} {local.day} {local.year}, "
        f"{hour12}:{local.minute:02d} {period}"
    )


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

    Dedupe for both mirror_event (the call lane) and mirror_message_event
    (the iMessage lane) is anchored entirely on the local note_mirror row
    keyed by event_id -- neither lane scans GHL note bodies for a marker;
    see the module docstring for the resulting recovery limitation if
    local state is lost, and why an ordinary collector retry still cannot
    create a duplicate note.
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
        """OPS-75 iMessage lane. See module docstring for the exact body/
        title/color contract and why there is no marker-search recovery
        path here."""
        existing = self._state_db.get_note_mirror(event_id)
        if existing is not None and existing.status == "complete" and existing.note_id:
            return self._verify_existing(existing.note_id, contact_id)

        title = format_imessage_note_title(occurred_at=occurred_at, is_from_me=is_from_me)
        color = IMESSAGE_SENT_COLOR if is_from_me else IMESSAGE_RECEIVED_COLOR
        body = format_imessage_note_body(text=text)
        return self._create_and_verify(
            event_id=event_id, contact_id=contact_id, body=body, trigger=trigger,
            color=color, title=title, redact_body_in_audit=True,
        )

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
    "IMESSAGE_SENT_COLOR",
    "OK_OUTCOMES",
    "MirrorResult",
    "NoteMirror",
    "contact_detail_url",
    "format_imessage_note_body",
    "format_imessage_note_title",
    "format_note_body",
]
