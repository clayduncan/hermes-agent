"""Two-path booking router for Clay's calendar.

Clay's explicit ruling
-----------------------
1. **In-hours** — Calendly POST /invitees if the exact slot appears as ``status:
   "available"`` in get_event_type_available_times.  Calendly's own Zoom
   integration attaches the join URL; its Office 365 Calendar Connection pushes
   the event to Outlook automatically.  No separate Graph call needed.

2. **Exception** (short notice or outside normal hours) — Zoom meeting creation
   (Server-to-Server OAuth) then Graph calendar event.  This path is fully
   audited via :class:`tools.msgraph_write_client.MicrosoftGraphCalendarEventsWriteClient`.

Safety invariant
----------------
A transient Calendly availability-check failure (network, 5xx, timeout) raises
and **does not** silently fall through to the exception path.  Only an explicit
"slot not in available collection" result triggers the fallback.  This is the
critical safety property: a Calendly outage must never quietly route meetings to
the exception path for slots that were actually available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tools.calendly_client import CalendlyBookingWriteClient, CalendlyClient
    from tools.msgraph_write_client import MicrosoftGraphCalendarEventsWriteClient
    from tools.zoom_client import ZoomClient


@dataclass
class BookingResult:
    """The outcome of a :func:`book_meeting` call."""

    path: Literal["calendly", "graph_zoom_exception"]
    calendly_event_uri: str | None = None
    zoom_join_url: str | None = None
    zoom_meeting_id: str | None = None
    graph_event_id: str | None = None


def book_meeting(
    *,
    calendly_client: "CalendlyClient",
    calendly_writer: "CalendlyBookingWriteClient",
    graph_calendar_writer: "MicrosoftGraphCalendarEventsWriteClient",
    zoom_client: "ZoomClient",
    event_type_uri: str,
    start_time_iso: str,
    duration_minutes: int,
    invitee_name: str,
    invitee_email: str,
    invitee_timezone: str,
    trigger: str,
) -> BookingResult:
    """Route a meeting booking to Calendly (in-hours) or Zoom+Graph (exception).

    Raises on any transient error from the Calendly availability check — never
    silently falls through to the exception path on uncertainty.

    Parameters
    ----------
    calendly_client:
        Read-only :class:`~tools.calendly_client.CalendlyClient` for availability queries.
    calendly_writer:
        Audited :class:`~tools.calendly_client.CalendlyBookingWriteClient` for the
        Calendly POST /invitees path.
    graph_calendar_writer:
        Audited Graph calendar events client for the exception path.
    zoom_client:
        Zoom client for creating the meeting on the exception path.
    event_type_uri:
        The Calendly event type URI to check availability against.
    start_time_iso:
        Requested start time in UTC ISO8601 (e.g. ``"2026-09-01T14:00:00Z"``).
    duration_minutes:
        Meeting duration used for the Zoom meeting and the Graph event end time.
    invitee_name:
        The invitee's display name (used in Graph event attendees and Zoom topic).
    invitee_email:
        The invitee's email address.
    invitee_timezone:
        The invitee's IANA timezone name (e.g. ``"America/Chicago"``).
    trigger:
        Human-readable audit trigger (required by both the Calendly and Graph audit gates).
    """
    from tools.calendly_client import CalendlySlotUnavailable

    # Step 1: Check Calendly availability.
    # Any exception here (network, 5xx, etc.) propagates — never silently fall through.
    day_start, day_end = _day_window_for(start_time_iso)
    available_times = calendly_client.get_event_type_available_times(
        event_type_uri, day_start, day_end
    )

    slot_is_available = _slot_in_collection(start_time_iso, available_times)

    if slot_is_available:
        # Step 2a: In-hours path — book via Calendly.
        invitee = {
            "name": invitee_name,
            "email": invitee_email,
            "timezone": invitee_timezone,
        }
        location = {"kind": "zoom_conference"}
        resource = calendly_writer.create_invitee(
            event_type_uri,
            start_time_iso,
            invitee,
            location,
            trigger=trigger,
        )
        event_uri = resource.get("event") or resource.get("uri")
        zoom_join_url = _extract_zoom_url_from_invitee(resource)
        return BookingResult(
            path="calendly",
            calendly_event_uri=event_uri,
            zoom_join_url=zoom_join_url,
        )

    # Step 2b: Exception path — slot not available in Calendly.
    end_time_iso = _add_minutes(start_time_iso, duration_minutes)

    zoom_meeting = zoom_client.create_meeting(
        topic=f"Meeting with {invitee_name}",
        start_time_iso=start_time_iso,
        timezone_name=invitee_timezone,
        duration_minutes=duration_minutes,
        invitee_emails=[invitee_email],
    )
    zoom_join_url: str | None = zoom_meeting.get("join_url")
    zoom_password: str | None = zoom_meeting.get("password")
    zoom_id = zoom_meeting.get("id")

    body_html = _build_exception_body_html(
        invitee_name=invitee_name,
        start_time_iso=start_time_iso,
        zoom_join_url=zoom_join_url or "",
        zoom_password=zoom_password,
    )

    attendees = [
        {
            "emailAddress": {"address": invitee_email, "name": invitee_name},
            "type": "required",
        }
    ]

    graph_event = graph_calendar_writer.create_event(
        subject=f"Meeting with {invitee_name}",
        start=start_time_iso.rstrip("Z"),
        end=end_time_iso.rstrip("Z"),
        attendees=attendees,
        body_html=body_html,
        location_display_name=zoom_join_url,
        trigger=trigger,
    )

    graph_event_id = graph_event.get("id") if isinstance(graph_event, dict) else None
    return BookingResult(
        path="graph_zoom_exception",
        zoom_join_url=zoom_join_url,
        zoom_meeting_id=str(zoom_id) if zoom_id is not None else None,
        graph_event_id=graph_event_id,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slot_in_collection(start_time_iso: str, available_times: list[dict]) -> bool:
    """Return True iff *start_time_iso* appears in *available_times* with status ``available``."""
    if not available_times:
        return False
    # Normalise to a comparable prefix (strip trailing Z / +00:00).
    needle = _normalise_iso(start_time_iso)
    for slot in available_times:
        status = slot.get("status", "")
        invitees_available = slot.get("invitees_remaining", 1)
        if status != "available" or not invitees_available:
            continue
        slot_time = _normalise_iso(slot.get("start_time") or "")
        if slot_time and slot_time == needle:
            return True
    return False


def _normalise_iso(ts: str) -> str:
    """Strip trailing ``Z`` and ``+00:00`` for equality comparison."""
    return ts.replace("Z", "").replace("+00:00", "").strip()


def _day_window_for(start_time_iso: str) -> tuple[str, str]:
    from tools.calendly_client import _day_window_for as _cwf
    return _cwf(start_time_iso)


def _add_minutes(start_time_iso: str, minutes: int) -> str:
    """Return *start_time_iso* + *minutes* as an ISO8601 UTC string."""
    from datetime import datetime, timedelta, timezone
    normalized = start_time_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    end = dt + timedelta(minutes=minutes)
    return end.strftime("%Y-%m-%dT%H:%M:%S")


def _extract_zoom_url_from_invitee(resource: dict) -> str | None:
    """Best-effort: find a Zoom join URL in a Calendly invitee resource."""
    location = resource.get("location") or {}
    if isinstance(location, dict):
        return location.get("join_url") or location.get("location")
    return None


def _build_exception_body_html(
    invitee_name: str,
    start_time_iso: str,
    zoom_join_url: str,
    zoom_password: str | None,
) -> str:
    lines = [
        f"<p>Hi {invitee_name},</p>",
        f"<p>Join Zoom: <a href=\"{zoom_join_url}\">{zoom_join_url}</a></p>",
    ]
    if zoom_password:
        lines.append(f"<p>Passcode: {zoom_password}</p>")
    lines.append("<p>This meeting was booked via the exception path (outside normal availability).</p>")
    return "\n".join(lines)


__all__ = ["BookingResult", "book_meeting"]
