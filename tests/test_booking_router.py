"""Tests for the two-path booking router.

Critical safety test: a Calendly availability-check network error must propagate
and NOT silently fall through to the exception path.  That test is the most
important one here — it verifies the core safety invariant Clay relies on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from tools.booking_router import BookingResult, book_meeting, _slot_in_collection


# ── Fake clients ──────────────────────────────────────────────────────────────


class FakeCalendlyClient:
    """Read-only Calendly fake — controls availability check and scheduled-event fetch."""

    def __init__(
        self,
        available_times: list[dict] | Exception,
        scheduled_event: dict | Exception | None = None,
    ) -> None:
        self._available_times = available_times
        # None → return {} (no Zoom link yet); Exception → raise it.
        self._scheduled_event = scheduled_event
        self.calls: list[tuple] = []
        self.event_fetch_calls: list[str] = []

    def get_event_type_available_times(
        self, event_type_uri: str, start_time: str, end_time: str
    ) -> list[dict]:
        self.calls.append((event_type_uri, start_time, end_time))
        if isinstance(self._available_times, Exception):
            raise self._available_times
        return self._available_times

    def get_scheduled_event(self, event_uri: str) -> dict:
        self.event_fetch_calls.append(event_uri)
        if isinstance(self._scheduled_event, Exception):
            raise self._scheduled_event
        if self._scheduled_event is None:
            return {}
        return self._scheduled_event


class FakeCalendlyWriter:
    """Audited Calendly booking write fake."""

    def __init__(self, response: dict | Exception) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def create_invitee(
        self,
        event_type_uri: str,
        start_time_iso: str,
        invitee: dict,
        location: dict,
        *,
        trigger: str,
    ) -> dict:
        self.calls.append((event_type_uri, start_time_iso, invitee, location, trigger))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeGraphCalendarWriter:
    """Audited Graph calendar events write fake."""

    def __init__(self, response: dict | None = None) -> None:
        self._response = response or {"id": "graph-event-1"}
        self.calls: list[tuple] = []

    def create_event(self, subject, start, end, attendees, *, trigger, **kwargs) -> dict:
        self.calls.append((subject, start, end, attendees, trigger))
        return self._response


class FakeZoomClient:
    """Zoom meeting creation fake."""

    def __init__(self, response: dict | None = None) -> None:
        self._response = response or {
            "id": 99999,
            "join_url": "https://zoom.us/j/99999",
            "password": "zoompass",
        }
        self.calls: list[tuple] = []

    def create_meeting(self, topic, start_time_iso, timezone_name, duration_minutes, *, invitee_emails, **kwargs) -> dict:
        self.calls.append((topic, start_time_iso, timezone_name, duration_minutes, invitee_emails))
        return self._response


# ── Constants ─────────────────────────────────────────────────────────────────

EVENT_TYPE_URI = "https://api.calendly.com/event_types/ABCDEF"
START_TIME = "2026-09-01T14:00:00Z"
TRIGGER = "router-test-2026-08-22"

AVAILABLE_SLOT = {"start_time": START_TIME, "status": "available", "invitees_remaining": 1}

# Real captured shape: POST /invitees response (2026-08-22 live session).
# NO "location" field — that lives on the scheduled_events resource, not here.
INVITEE_RESOURCE = {
    "cancel_url": "https://calendly.com/cancellations/8b5e2bc5-52bb-4dc1-8ad0-41ef0d6084b8",
    "created_at": "2026-08-22T17:13:17.150860Z",
    "email": "lduncan@princetonmortgage.com",
    "event": "https://api.calendly.com/scheduled_events/cdd4c933-653b-4373-937a-3f9b08111a4a",
    "first_name": "Levi",
    "last_name": "Duncan",
    "name": "Levi Duncan",
    "new_invitee": None,
    "no_show": None,
    "questions_and_answers": [],
    "rescheduled": False,
    "scheduling_method": "api",
    "status": "active",
    "timezone": "America/Chicago",
    "updated_at": "2026-08-22T17:13:17.150860Z",
    "uri": "https://api.calendly.com/scheduled_events/cdd4c933-653b-4373-937a-3f9b08111a4a/invitees/8b5e2bc5-52bb-4dc1-8ad0-41ef0d6084b8",
}

# Real captured shape: GET /scheduled_events/{uuid} response (2026-08-22 live session).
# location.status="pushed" means Calendly's Zoom integration has finished attaching.
SCHEDULED_EVENT_UUID = "cdd4c933-653b-4373-937a-3f9b08111a4a"
SCHEDULED_EVENT_RESOURCE = {
    "calendar_event": {"external_id": "AAMkAGZk...", "kind": "outlook"},
    "created_at": "2026-08-22T17:13:17.126040Z",
    "end_time": "2026-08-25T14:00:00.000000Z",
    "event_type": "https://api.calendly.com/event_types/9ac0b557-1840-4846-b35b-cc457712510b",
    "invitees_counter": {"active": 1, "limit": 1, "total": 1},
    "location": {
        "data": {"id": 82003739767, "password": "833132"},
        "join_url": "https://us06web.zoom.us/j/82003739767?pwd=cWTCn392fefLbLZb5IC1XzvcD1XTjC.1",
        "status": "pushed",
        "type": "zoom",
    },
    "name": "Mortgage Talk",
    "start_time": "2026-08-25T13:00:00.000000Z",
    "status": "active",
    "uri": f"https://api.calendly.com/scheduled_events/{SCHEDULED_EVENT_UUID}",
}


def _default_calendly_client() -> FakeCalendlyClient:
    """Default read-only fake: slot available, Zoom integration attached (pushed)."""
    return FakeCalendlyClient([AVAILABLE_SLOT], scheduled_event=SCHEDULED_EVENT_RESOURCE)


def _common_kwargs(
    calendly_client=None,
    calendly_writer=None,
    graph_writer=None,
    zoom_client=None,
) -> dict:
    return {
        "calendly_client": calendly_client or _default_calendly_client(),
        "calendly_writer": calendly_writer or FakeCalendlyWriter(INVITEE_RESOURCE),
        "graph_calendar_writer": graph_writer or FakeGraphCalendarWriter(),
        "zoom_client": zoom_client or FakeZoomClient(),
        "event_type_uri": EVENT_TYPE_URI,
        "start_time_iso": START_TIME,
        "duration_minutes": 30,
        "invitee_name": "Alice",
        "invitee_email": "alice@example.com",
        "invitee_timezone": "UTC",
        "trigger": TRIGGER,
    }


# ── Path routing ──────────────────────────────────────────────────────────────


class TestBookingRouterPaths:
    def test_available_slot_routes_to_calendly(self) -> None:
        calendly_client = FakeCalendlyClient([AVAILABLE_SLOT], scheduled_event=SCHEDULED_EVENT_RESOURCE)
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)
        graph_writer = FakeGraphCalendarWriter()
        zoom_client = FakeZoomClient()

        result = book_meeting(**_common_kwargs(
            calendly_client=calendly_client,
            calendly_writer=calendly_writer,
            graph_writer=graph_writer,
            zoom_client=zoom_client,
        ))

        assert result.path == "calendly"
        assert result.calendly_event_uri is not None
        assert len(calendly_writer.calls) == 1
        assert len(graph_writer.calls) == 0
        assert len(zoom_client.calls) == 0

    def test_available_slot_never_touches_zoom_or_graph(self) -> None:
        zoom_client = FakeZoomClient()
        graph_writer = FakeGraphCalendarWriter()

        book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([AVAILABLE_SLOT]),
            zoom_client=zoom_client,
            graph_writer=graph_writer,
        ))

        assert zoom_client.calls == []
        assert graph_writer.calls == []

    def test_unavailable_slot_routes_to_zoom_plus_graph(self) -> None:
        calendly_client = FakeCalendlyClient([])  # empty = not available
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)
        graph_writer = FakeGraphCalendarWriter()
        zoom_client = FakeZoomClient()

        result = book_meeting(**_common_kwargs(
            calendly_client=calendly_client,
            calendly_writer=calendly_writer,
            graph_writer=graph_writer,
            zoom_client=zoom_client,
        ))

        assert result.path == "graph_zoom_exception"
        assert result.zoom_join_url == "https://zoom.us/j/99999"
        assert result.graph_event_id == "graph-event-1"
        # Calendly booking write must NOT be called
        assert calendly_writer.calls == []

    def test_unavailable_slot_never_touches_calendly_write(self) -> None:
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)

        book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([]),
            calendly_writer=calendly_writer,
        ))

        assert calendly_writer.calls == []

    def test_slot_not_matching_exact_time_routes_to_exception(self) -> None:
        # Available collection exists but the specific slot time doesn't match.
        different_slot = {"start_time": "2026-09-01T15:00:00Z", "status": "available", "invitees_remaining": 1}
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)
        zoom_client = FakeZoomClient()

        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([different_slot]),
            calendly_writer=calendly_writer,
            zoom_client=zoom_client,
        ))

        assert result.path == "graph_zoom_exception"
        assert calendly_writer.calls == []

    def test_slot_with_unavailable_status_routes_to_exception(self) -> None:
        busy_slot = {"start_time": START_TIME, "status": "unavailable", "invitees_remaining": 0}
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)

        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([busy_slot]),
            calendly_writer=calendly_writer,
        ))

        assert result.path == "graph_zoom_exception"
        assert calendly_writer.calls == []


# ── The critical safety test ──────────────────────────────────────────────────


class TestCalendlyAvailabilityCheckErrorPropagates:
    """A transient Calendly availability-check failure must propagate — never fall through.

    This is Clay's explicit safety requirement: if the availability check itself
    fails (network error, 5xx, timeout), the router must NOT silently fall back
    to the exception path for a slot that may well have been available.  Only an
    explicit empty-or-non-matching collection triggers the fallback.
    """

    def test_network_error_in_availability_check_propagates(self) -> None:
        error = ConnectionError("Calendly is unreachable")
        calendly_client = FakeCalendlyClient(error)
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)
        zoom_client = FakeZoomClient()
        graph_writer = FakeGraphCalendarWriter()

        with pytest.raises(ConnectionError, match="Calendly is unreachable"):
            book_meeting(**_common_kwargs(
                calendly_client=calendly_client,
                calendly_writer=calendly_writer,
                zoom_client=zoom_client,
                graph_writer=graph_writer,
            ))

    def test_network_error_does_not_call_zoom_or_graph(self) -> None:
        zoom_client = FakeZoomClient()
        graph_writer = FakeGraphCalendarWriter()

        with pytest.raises(ConnectionError):
            book_meeting(**_common_kwargs(
                calendly_client=FakeCalendlyClient(ConnectionError("timeout")),
                zoom_client=zoom_client,
                graph_writer=graph_writer,
            ))

        assert zoom_client.calls == [], "Zoom must not be called on a Calendly availability error"
        assert graph_writer.calls == [], "Graph must not be called on a Calendly availability error"

    def test_network_error_does_not_call_calendly_write(self) -> None:
        calendly_writer = FakeCalendlyWriter(INVITEE_RESOURCE)

        with pytest.raises(RuntimeError):
            book_meeting(**_common_kwargs(
                calendly_client=FakeCalendlyClient(RuntimeError("5xx from Calendly")),
                calendly_writer=calendly_writer,
            ))

        assert calendly_writer.calls == [], "Calendly write must not be called on an availability check error"

    def test_exception_type_is_preserved_not_wrapped(self) -> None:
        """The original exception type must propagate unchanged."""
        class SpecificError(Exception):
            pass

        with pytest.raises(SpecificError):
            book_meeting(**_common_kwargs(
                calendly_client=FakeCalendlyClient(SpecificError("unique error")),
            ))


# ── BookingResult fields ──────────────────────────────────────────────────────


class TestBookingResultFields:
    def test_calendly_path_result_has_event_uri(self) -> None:
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([AVAILABLE_SLOT], scheduled_event=SCHEDULED_EVENT_RESOURCE),
            calendly_writer=FakeCalendlyWriter(INVITEE_RESOURCE),
        ))
        assert result.path == "calendly"
        assert result.calendly_event_uri is not None
        assert result.graph_event_id is None

    def test_exception_path_result_has_zoom_and_graph_ids(self) -> None:
        zoom_resp = {"id": 77777, "join_url": "https://zoom.us/j/77777", "password": "pw"}
        graph_resp = {"id": "gevent-7"}
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([]),
            zoom_client=FakeZoomClient(zoom_resp),
            graph_writer=FakeGraphCalendarWriter(graph_resp),
        ))
        assert result.path == "graph_zoom_exception"
        assert result.zoom_join_url == "https://zoom.us/j/77777"
        assert result.zoom_meeting_id == "77777"
        assert result.graph_event_id == "gevent-7"
        assert result.calendly_event_uri is None

    def test_calendly_path_extracts_zoom_url_from_scheduled_event(self) -> None:
        """Zoom join URL comes from GET /scheduled_events/{uuid}, not the invitee resource.

        Real captured shape (2026-08-22 live session): the invitee POST response has
        no location field; the scheduled_events GET response has location.join_url when
        Calendly's Zoom integration has finished (location.status == "pushed").
        """
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient(
                [AVAILABLE_SLOT],
                scheduled_event=SCHEDULED_EVENT_RESOURCE,
            ),
            calendly_writer=FakeCalendlyWriter(INVITEE_RESOURCE),
        ))
        assert result.zoom_join_url == "https://us06web.zoom.us/j/82003739767?pwd=cWTCn392fefLbLZb5IC1XzvcD1XTjC.1"

    def test_calendly_path_zoom_url_is_none_when_location_absent(self) -> None:
        """When Calendly's Zoom integration has not yet attached (location absent),
        zoom_join_url is None and the booking is still reported as succeeded."""
        event_without_location = {**SCHEDULED_EVENT_RESOURCE}
        event_without_location.pop("location", None)
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient(
                [AVAILABLE_SLOT],
                scheduled_event=event_without_location,
            ),
            calendly_writer=FakeCalendlyWriter(INVITEE_RESOURCE),
        ))
        assert result.path == "calendly"
        assert result.zoom_join_url is None
        assert result.calendly_event_uri is not None

    def test_calendly_path_zoom_url_is_none_when_scheduled_event_fetch_raises(self) -> None:
        """A network error on the follow-up GET /scheduled_events must not fail
        the booking — create_invitee already succeeded, this is a best-effort read.

        This is the key resilience test: the booking must succeed even if Calendly's
        scheduled_events API is temporarily unreachable immediately after booking.
        """
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient(
                [AVAILABLE_SLOT],
                scheduled_event=ConnectionError("Calendly scheduled_events unreachable"),
            ),
            calendly_writer=FakeCalendlyWriter(INVITEE_RESOURCE),
        ))
        assert result.path == "calendly"
        assert result.zoom_join_url is None
        assert result.calendly_event_uri is not None


# ── _slot_in_collection unit tests ────────────────────────────────────────────


class TestSlotInCollection:
    def test_exact_match_returns_true(self) -> None:
        slots = [{"start_time": "2026-09-01T14:00:00Z", "status": "available", "invitees_remaining": 1}]
        assert _slot_in_collection("2026-09-01T14:00:00Z", slots) is True

    def test_no_match_returns_false(self) -> None:
        slots = [{"start_time": "2026-09-01T15:00:00Z", "status": "available", "invitees_remaining": 1}]
        assert _slot_in_collection("2026-09-01T14:00:00Z", slots) is False

    def test_empty_collection_returns_false(self) -> None:
        assert _slot_in_collection("2026-09-01T14:00:00Z", []) is False

    def test_unavailable_status_returns_false(self) -> None:
        slots = [{"start_time": "2026-09-01T14:00:00Z", "status": "unavailable", "invitees_remaining": 0}]
        assert _slot_in_collection("2026-09-01T14:00:00Z", slots) is False

    def test_z_suffix_normalisation(self) -> None:
        # "Z" and "+00:00" variants should match each other.
        slots = [{"start_time": "2026-09-01T14:00:00+00:00", "status": "available", "invitees_remaining": 1}]
        assert _slot_in_collection("2026-09-01T14:00:00Z", slots) is True
