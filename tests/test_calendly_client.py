"""Tests for CalendlyClient (read-only) and CalendlyBookingWriteClient (audited).

The load-bearing class here is :class:`TestLogFailureBlocksCalendlyWrite`, which
mirrors :class:`tests.test_msgraph_write_client.TestLogFailureBlocksTheWriteEntirely`.
Its purpose: a failed audit append must block POST /invitees entirely — the
destination API must never be called unless the audit line is fsync'd first.

All tests use the SpyTransport fake; no live network calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import tools.write_audit_log as write_audit_log
from tests.fakes.write_audit_http import SpyTransport, json_response
from tools.calendly_client import (
    DEFAULT_CALENDLY_BASE_URL,
    CalendlyAPIError,
    CalendlyBookingWriteClient,
    CalendlyClient,
    CalendlyRecapsUnavailable,
    CalendlySlotUnavailable,
)
from tools.sync_json_http import HttpResponse
from tools.write_audit_log import (
    MissingWriteTriggerError,
    WriteAuditLogError,
    iter_entries,
)

TRIGGER = "clay-calendly-booking-test-2026-08-22"
EVENT_TYPE_URI = "https://api.calendly.com/event_types/ABCDEF"
START_TIME = "2026-09-01T14:00:00Z"
DAY_START = "2026-09-01T00:00:00Z"
DAY_END = "2026-09-01T23:59:59Z"

AVAILABLE_TIMES_RESPONSE = {
    "collection": [
        {"start_time": START_TIME, "status": "available", "invitees_remaining": 1},
    ]
}

# Real captured shape from POST /invitees live session 2026-08-22.
# Notably: NO "location" key on the invitee resource.  The Zoom join URL lives
# on the scheduled_events resource, not here.
INVITEE_RESOURCE = {
    "cancel_url": "https://calendly.com/cancellations/8b5e2bc5-52bb-4dc1-8ad0-41ef0d6084b8",
    "created_at": "2026-08-22T17:13:17.150860Z",
    "email": "lduncan@princetonmortgage.com",
    "event": "https://api.calendly.com/scheduled_events/cdd4c933-653b-4373-937a-3f9b08111a4a",
    "first_name": "Levi",
    "invitee_scheduled_by": "https://api.calendly.com/users/a613a1bb-a118-46c0-b518-7d2795909c2c",
    "last_name": "Duncan",
    "name": "Levi Duncan",
    "new_invitee": None,
    "no_show": None,
    "old_invitee": None,
    "payment": None,
    "questions_and_answers": [],
    "reconfirmation": None,
    "reschedule_url": "https://calendly.com/reschedulings/8b5e2bc5-52bb-4dc1-8ad0-41ef0d6084b8",
    "rescheduled": False,
    "routing_form_submission": None,
    "scheduling_method": "api",
    "status": "active",
    "text_reminder_number": None,
    "timezone": "America/Chicago",
    "tracking": {
        "utm_campaign": None, "utm_source": None, "utm_medium": None,
        "utm_content": None, "utm_term": None, "salesforce_uuid": None,
    },
    "updated_at": "2026-08-22T17:13:17.150860Z",
    "uri": "https://api.calendly.com/scheduled_events/cdd4c933-653b-4373-937a-3f9b08111a4a/invitees/8b5e2bc5-52bb-4dc1-8ad0-41ef0d6084b8",
}
INVITEE_BODY = {"resource": INVITEE_RESOURCE}

# Real captured shape from GET /scheduled_events/{uuid} live session 2026-08-22.
# "location.status": "pushed" means Calendly's Zoom integration has finished.
SCHEDULED_EVENT_UUID = "cdd4c933-653b-4373-937a-3f9b08111a4a"
SCHEDULED_EVENT_RESOURCE = {
    "calendar_event": {
        "external_id": "AAMkAGZkNjM2NDA0LTQ1YTktNDgzNy04MzgxLTE0ODE5NTVjOTBlYQ==",
        "kind": "outlook",
    },
    "created_at": "2026-08-22T17:13:17.126040Z",
    "end_time": "2026-08-25T14:00:00.000000Z",
    "event_guests": [],
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
    "updated_at": "2026-08-22T17:13:25.597236Z",
    "uri": f"https://api.calendly.com/scheduled_events/{SCHEDULED_EVENT_UUID}",
}
SCHEDULED_EVENT_BODY = {"resource": SCHEDULED_EVENT_RESOURCE}

USER_RESOURCE = {"resource": {"uri": "https://api.calendly.com/users/ME", "name": "Clay"}}
EVENT_TYPE_DATA = {"resource": {"uri": EVENT_TYPE_URI, "name": "30 Minute Meeting"}}
SCHEDULED_EVENTS_PAGE1 = {
    "collection": [{"uri": "https://api.calendly.com/scheduled_events/E1"}],
    "pagination": {"count": 1, "next_page_token": "tok123"},
}
SCHEDULED_EVENTS_PAGE2 = {
    "collection": [{"uri": "https://api.calendly.com/scheduled_events/E2"}],
    "pagination": {"count": 1},
}


def entries(log_dir: Path) -> list[dict]:
    return [entry for _, _, entry in iter_entries(log_dir)]


def outcome_entry(log_dir: Path) -> dict:
    found = [e for e in entries(log_dir) if e["audit_phase"] == "outcome"]
    assert len(found) == 1, f"expected exactly one outcome entry, got {found}"
    return found[0]


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "write_audit"


@pytest.fixture
def transport() -> SpyTransport:
    return SpyTransport(DEFAULT_CALENDLY_BASE_URL)


def read_client(transport: SpyTransport) -> CalendlyClient:
    return CalendlyClient(
        lambda: "fake-pat",
        request_fn=transport,
        sleep=lambda _: None,
    )


def write_client(transport: SpyTransport, log_dir: Path, **kwargs) -> CalendlyBookingWriteClient:
    return CalendlyBookingWriteClient(
        lambda: "fake-pat",
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _: None,
        **kwargs,
    )


def route_available_times(transport: SpyTransport) -> None:
    transport.route("GET", "/event_type_available_times", AVAILABLE_TIMES_RESPONSE)


def route_create_invitee(transport: SpyTransport) -> None:
    transport.route("POST", "/invitees", HttpResponse(status_code=201, body=json.dumps(INVITEE_BODY).encode()))


# ── CalendlyClient read-only tests ────────────────────────────────────────────


class TestCalendlyClientReads:
    def test_get_current_user(self, transport: SpyTransport) -> None:
        transport.route("GET", "/users/me", USER_RESOURCE)
        result = read_client(transport).get_current_user()
        assert result == USER_RESOURCE["resource"]

    def test_get_event_type_bare_uuid(self, transport: SpyTransport) -> None:
        transport.route("GET", "/event_types/ABCDEF", EVENT_TYPE_DATA)
        result = read_client(transport).get_event_type("ABCDEF")
        assert result == EVENT_TYPE_DATA["resource"]

    def test_get_event_type_full_uri(self, transport: SpyTransport) -> None:
        transport.route("GET", "/event_types/ABCDEF", EVENT_TYPE_DATA)
        result = read_client(transport).get_event_type(EVENT_TYPE_URI)
        assert result == EVENT_TYPE_DATA["resource"]

    def test_list_event_types_single_page(self, transport: SpyTransport) -> None:
        transport.route("GET", "/event_types", {"collection": [{"uri": "et-1"}], "pagination": {}})
        result = read_client(transport).list_event_types("https://api.calendly.com/users/ME")
        assert result == [{"uri": "et-1"}]

    def test_get_event_type_available_times(self, transport: SpyTransport) -> None:
        transport.route("GET", "/event_type_available_times", AVAILABLE_TIMES_RESPONSE)
        result = read_client(transport).get_event_type_available_times(
            EVENT_TYPE_URI, DAY_START, DAY_END
        )
        assert result == AVAILABLE_TIMES_RESPONSE["collection"]

    def test_get_user_availability_schedules(self, transport: SpyTransport) -> None:
        resp = {"collection": [{"uuid": "sched-1"}]}
        transport.route("GET", "/user_availability_schedules", resp)
        result = read_client(transport).get_user_availability_schedules("https://api.calendly.com/users/ME")
        assert result == [{"uuid": "sched-1"}]

    def test_list_scheduled_events_single_page(self, transport: SpyTransport) -> None:
        resp = {"collection": [{"uri": "se-1"}], "pagination": {}}
        transport.route("GET", "/scheduled_events", resp)
        result = read_client(transport).list_scheduled_events(
            "https://api.calendly.com/users/ME", DAY_START, DAY_END
        )
        assert result == [{"uri": "se-1"}]

    def test_pagination_follows_next_page_token(self, transport: SpyTransport) -> None:
        transport.route("GET", "/scheduled_events", SCHEDULED_EVENTS_PAGE1, SCHEDULED_EVENTS_PAGE2)
        result = read_client(transport).list_scheduled_events(
            "https://api.calendly.com/users/ME", DAY_START, DAY_END
        )
        assert len(result) == 2
        uris = [e["uri"] for e in result]
        assert "https://api.calendly.com/scheduled_events/E1" in uris
        assert "https://api.calendly.com/scheduled_events/E2" in uris

    def test_get_event_invitees(self, transport: SpyTransport) -> None:
        resp = {"collection": [{"email": "a@b.com"}], "pagination": {}}
        transport.route("GET", "/scheduled_events/EVT1/invitees", resp)
        result = read_client(transport).get_event_invitees("EVT1")
        assert result == [{"email": "a@b.com"}]

    def test_get_scheduled_event_bare_uuid(self, transport: SpyTransport) -> None:
        transport.route("GET", f"/scheduled_events/{SCHEDULED_EVENT_UUID}", SCHEDULED_EVENT_BODY)
        result = read_client(transport).get_scheduled_event(SCHEDULED_EVENT_UUID)
        assert result == SCHEDULED_EVENT_RESOURCE
        assert result["location"]["join_url"] == "https://us06web.zoom.us/j/82003739767?pwd=cWTCn392fefLbLZb5IC1XzvcD1XTjC.1"
        assert result["location"]["status"] == "pushed"
        assert result["location"]["type"] == "zoom"

    def test_get_scheduled_event_full_uri(self, transport: SpyTransport) -> None:
        full_uri = f"https://api.calendly.com/scheduled_events/{SCHEDULED_EVENT_UUID}"
        transport.route("GET", f"/scheduled_events/{SCHEDULED_EVENT_UUID}", SCHEDULED_EVENT_BODY)
        result = read_client(transport).get_scheduled_event(full_uri)
        assert result == SCHEDULED_EVENT_RESOURCE

    def test_get_scheduled_event_no_location_returns_empty_location(self, transport: SpyTransport) -> None:
        event_without_zoom = {
            "resource": {
                "uri": f"https://api.calendly.com/scheduled_events/{SCHEDULED_EVENT_UUID}",
                "status": "active",
                # No "location" key — Zoom integration not yet attached
            }
        }
        transport.route("GET", f"/scheduled_events/{SCHEDULED_EVENT_UUID}", event_without_zoom)
        result = read_client(transport).get_scheduled_event(SCHEDULED_EVENT_UUID)
        assert result.get("location") is None

    def test_list_meeting_recaps_raises_typed_error_on_403(self, transport: SpyTransport) -> None:
        transport.route("GET", "/scheduling_links", HttpResponse(status_code=403, body=b"Forbidden"))
        with pytest.raises(CalendlyRecapsUnavailable) as excinfo:
            read_client(transport).list_meeting_recaps("https://api.calendly.com/users/ME")
        assert excinfo.value.status_code == 403

    def test_list_meeting_recaps_raises_typed_error_on_404(self, transport: SpyTransport) -> None:
        transport.route("GET", "/scheduling_links", HttpResponse(status_code=404, body=b"Not Found"))
        with pytest.raises(CalendlyRecapsUnavailable) as excinfo:
            read_client(transport).list_meeting_recaps("https://api.calendly.com/users/ME")
        assert excinfo.value.status_code == 404

    def test_list_meeting_recaps_non_403_404_reraises(self, transport: SpyTransport) -> None:
        transport.route("GET", "/scheduling_links", HttpResponse(status_code=500, body=b"Error"))
        with pytest.raises(Exception) as excinfo:
            read_client(transport).list_meeting_recaps("https://api.calendly.com/users/ME")
        # Should NOT be CalendlyRecapsUnavailable — it's a different error
        assert not isinstance(excinfo.value, CalendlyRecapsUnavailable)

    def test_request_carries_bearer_token(self, transport: SpyTransport) -> None:
        transport.route("GET", "/users/me", USER_RESOURCE)
        read_client(transport).get_current_user()
        assert transport.calls[0].headers["Authorization"] == "Bearer fake-pat"

    def test_request_carries_user_agent(self, transport: SpyTransport) -> None:
        transport.route("GET", "/users/me", USER_RESOURCE)
        read_client(transport).get_current_user()
        ua = transport.calls[0].headers.get("User-Agent", "")
        # Must not be the default Python urllib UA (which Cloudflare blocks).
        assert "Python" not in ua
        assert len(ua) > 10


# ── Audited write: happy path ─────────────────────────────────────────────────


class TestCalendlyBookingWriteHappyPath:
    def test_create_invitee_returns_resource(self, transport: SpyTransport, log_dir: Path) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        result = write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI,
            START_TIME,
            {"name": "Test Person", "email": "test@example.com", "timezone": "UTC"},
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        assert result == INVITEE_RESOURCE

    def test_create_invitee_writes_two_audit_lines(self, transport: SpyTransport, log_dir: Path) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI, START_TIME,
            {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        all_entries = entries(log_dir)
        assert len(all_entries) == 2
        assert all_entries[0]["audit_phase"] == "intent"
        assert all_entries[1]["audit_phase"] == "outcome"

    def test_before_is_the_available_times_snapshot(self, transport: SpyTransport, log_dir: Path) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI, START_TIME,
            {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        outcome = outcome_entry(log_dir)
        assert outcome["before"] == AVAILABLE_TIMES_RESPONSE["collection"]

    def test_after_is_the_created_invitee_resource(self, transport: SpyTransport, log_dir: Path) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI, START_TIME,
            {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == INVITEE_RESOURCE
        assert outcome["destination"] == "calendly_bookings"
        assert outcome["actor"] == "calendly_booking_client"
        assert outcome["trigger"] == TRIGGER

    def test_get_before_available_times_precedes_post(self, transport: SpyTransport, log_dir: Path) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI, START_TIME,
            {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        methods = [method for method, _ in transport.paths]
        assert methods[0] == "GET"
        assert "POST" in methods
        assert methods.index("GET") < methods.index("POST")

    def test_post_invitees_request_body_matches_real_captured_shape(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        """The outgoing POST /invitees body must match the real live payload that produced
        a 201 Created from Calendly on 2026-08-22 (Levi Duncan / Mortgage Talk booking).

        Real confirmed body (four flat top-level keys):
          event_type  — plain URI string, NOT an object
          start_time  — ISO8601 string
          invitee     — object with name/email/timezone
          location    — sibling object, NOT nested inside event_type
        """
        route_available_times(transport)
        route_create_invitee(transport)
        invitee_payload = {
            "name": "Levi Duncan",
            "first_name": "Levi",
            "last_name": "Duncan",
            "email": "lduncan@princetonmortgage.com",
            "timezone": "America/Chicago",
        }
        write_client(transport, log_dir).create_invitee(
            EVENT_TYPE_URI,
            START_TIME,
            invitee_payload,
            {"kind": "zoom_conference"},
            trigger=TRIGGER,
        )
        post_call = next(c for c in transport.calls if c.method == "POST")
        body = post_call.json_body
        # event_type must be a plain URI string, not an object
        assert body["event_type"] == EVENT_TYPE_URI, (
            f"event_type must be a plain URI string; got {body['event_type']!r}"
        )
        # location must be a top-level sibling, not nested inside event_type
        assert body["location"] == {"kind": "zoom_conference"}, (
            f"location must be a top-level key; got {body.get('location')!r}"
        )
        assert body["start_time"] == START_TIME
        assert body["invitee"] == invitee_payload
        # Confirm the old wrong shape is absent: event_type must not be a dict
        assert not isinstance(body["event_type"], dict), (
            "event_type must not be an object — it was incorrectly set to "
            f"{{'location': ...}} in the original implementation"
        )


# ── already_filled → CalendlySlotUnavailable ─────────────────────────────────


class TestAlreadyFilledMapping:
    def test_400_already_filled_raises_slot_unavailable(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_available_times(transport)
        body = json.dumps({"code": "already_filled", "message": "The slot is fully booked."}).encode()
        transport.route("POST", "/invitees", HttpResponse(status_code=400, body=body))
        with pytest.raises(CalendlySlotUnavailable) as excinfo:
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )
        assert excinfo.value.event_type_uri == EVENT_TYPE_URI
        assert excinfo.value.start_time_iso == START_TIME

    def test_400_already_filled_does_not_produce_outcome_entry(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_available_times(transport)
        body = json.dumps({"code": "already_filled", "message": "Full."}).encode()
        transport.route("POST", "/invitees", HttpResponse(status_code=400, body=body))
        with pytest.raises(CalendlySlotUnavailable):
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )
        all_entries = entries(log_dir)
        # Intent line was written (the write was authorized), but no outcome line.
        assert len(all_entries) == 1
        assert all_entries[0]["audit_phase"] == "intent"

    def test_400_other_code_raises_generic_api_error(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_available_times(transport)
        body = json.dumps({"code": "invalid_argument", "message": "Bad request."}).encode()
        transport.route("POST", "/invitees", HttpResponse(status_code=400, body=body))
        with pytest.raises(CalendlyAPIError) as excinfo:
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )
        assert not isinstance(excinfo.value, CalendlySlotUnavailable)
        assert excinfo.value.status_code == 400


# ── The load-bearing gate test ────────────────────────────────────────────────


class TestLogFailureBlocksCalendlyWrite:
    """A failed audit append must block POST /invitees entirely.

    This mirrors TestLogFailureBlocksTheWriteEntirely from test_msgraph_write_client.py.
    """

    def test_append_raising_blocks_the_invitees_post(
        self, transport: SpyTransport, log_dir: Path, monkeypatch
    ) -> None:
        route_available_times(transport)
        route_create_invitee(transport)

        def exploding_append(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(write_audit_log, "append_entry", exploding_append)

        with pytest.raises(WriteAuditLogError) as excinfo:
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )

        assert transport.writes == [], (
            f"POST /invitees was called despite audit failure: {transport.writes}"
        )
        message = str(excinfo.value)
        assert "NOT attempted" in message
        assert "No space left on device" in message
        assert TRIGGER in message

    def test_unwritable_log_dir_blocks_the_post(
        self, transport: SpyTransport, tmp_path: Path
    ) -> None:
        import os
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")

        route_available_times(transport)
        route_create_invitee(transport)

        read_only = tmp_path / "readonly"
        read_only.mkdir()
        os.chmod(read_only, 0o500)
        try:
            with pytest.raises(WriteAuditLogError):
                write_client(transport, read_only).create_invitee(
                    EVENT_TYPE_URI, START_TIME,
                    {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                    {"kind": "zoom_conference"},
                    trigger=TRIGGER,
                )
        finally:
            os.chmod(read_only, 0o700)

        assert transport.writes == []

    def test_log_failure_blocks_first_attempt_not_just_last(
        self, transport: SpyTransport, log_dir: Path, monkeypatch
    ) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        transport.force_mutating_responses(HttpResponse(status_code=503))

        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("audit log unavailable")),
        )

        with pytest.raises(WriteAuditLogError):
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )

        assert transport.writes == []

    def test_blocked_write_leaves_no_partial_log_entry(
        self, transport: SpyTransport, log_dir: Path, monkeypatch
    ) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        with pytest.raises(WriteAuditLogError):
            write_client(transport, log_dir).create_invitee(
                EVENT_TYPE_URI, START_TIME,
                {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                {"kind": "zoom_conference"},
                trigger=TRIGGER,
            )
        assert entries(log_dir) == []


# ── Trigger enforcement ───────────────────────────────────────────────────────


class TestTriggerRequiredForCalendlyWrite:
    def test_blank_trigger_blocks_before_any_http(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_available_times(transport)
        route_create_invitee(transport)
        for bad_trigger in ("", "  ", None):
            with pytest.raises(MissingWriteTriggerError):
                write_client(transport, log_dir).create_invitee(
                    EVENT_TYPE_URI, START_TIME,
                    {"name": "Test", "email": "t@t.com", "timezone": "UTC"},
                    {"kind": "zoom_conference"},
                    trigger=bad_trigger,  # type: ignore[arg-type]
                )
        assert transport.calls == []
        assert entries(log_dir) == []
