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
    """Read-only Calendly fake — controls what get_event_type_available_times returns."""

    def __init__(self, available_times: list[dict] | Exception) -> None:
        self._available_times = available_times
        self.calls: list[tuple] = []

    def get_event_type_available_times(
        self, event_type_uri: str, start_time: str, end_time: str
    ) -> list[dict]:
        self.calls.append((event_type_uri, start_time, end_time))
        if isinstance(self._available_times, Exception):
            raise self._available_times
        return self._available_times


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
INVITEE_RESOURCE = {
    "uri": "https://api.calendly.com/scheduled_events/XYZ/invitees/INV1",
    "event": "https://api.calendly.com/scheduled_events/XYZ",
    "location": {"kind": "zoom_conference", "join_url": "https://zoom.us/j/cal123"},
}


def _common_kwargs(
    calendly_client=None,
    calendly_writer=None,
    graph_writer=None,
    zoom_client=None,
) -> dict:
    return {
        "calendly_client": calendly_client or FakeCalendlyClient([AVAILABLE_SLOT]),
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
        calendly_client = FakeCalendlyClient([AVAILABLE_SLOT])
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
            calendly_client=FakeCalendlyClient([AVAILABLE_SLOT]),
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

    def test_calendly_path_extracts_zoom_url_from_location(self) -> None:
        invitee_with_zoom = {
            "uri": "inv-1",
            "event": "evt-1",
            "location": {"kind": "zoom_conference", "join_url": "https://zoom.us/j/cal999"},
        }
        result = book_meeting(**_common_kwargs(
            calendly_client=FakeCalendlyClient([AVAILABLE_SLOT]),
            calendly_writer=FakeCalendlyWriter(invitee_with_zoom),
        ))
        assert result.zoom_join_url == "https://zoom.us/j/cal999"


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
