"""Tests for MicrosoftGraphCalendarEventsWriteClient.

Mirrors the shape of test_msgraph_write_client.py — happy path before/after
correctness, the load-bearing gate tests (audit failure blocks the destination),
trigger enforcement, and fetch-after failure handling.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.write_audit_log as write_audit_log
from tests.fakes.write_audit_http import SpyTransport, json_response
from tools.msgraph_write_client import (
    DEFAULT_GRAPH_BASE_URL,
    MicrosoftGraphCalendarEventsWriteClient,
)
from tools.sync_json_http import HttpResponse
from tools.write_audit_log import (
    AFTER_UNKNOWN,
    MissingWriteTriggerError,
    WriteAuditLogError,
    iter_entries,
)

TRIGGER = "exception-booking-test-2026-08-22"
START = "2026-09-01T14:00:00"
END = "2026-09-01T14:30:00"
ATTENDEES = [{"emailAddress": {"address": "alice@example.com", "name": "Alice"}, "type": "required"}]

CALENDAR_VIEW_BEFORE = {"value": []}  # no conflicts
EVENT_CREATED = {
    "id": "event-1",
    "subject": "Meeting with Alice",
    "start": {"dateTime": START, "timeZone": "UTC"},
    "end": {"dateTime": END, "timeZone": "UTC"},
}
EVENT_AFTER = {**EVENT_CREATED, "webLink": "https://outlook.office.com/calendar/item/event-1"}


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
    return SpyTransport(DEFAULT_GRAPH_BASE_URL)


def calendar_client(transport: SpyTransport, log_dir: Path, **kwargs) -> MicrosoftGraphCalendarEventsWriteClient:
    return MicrosoftGraphCalendarEventsWriteClient(
        lambda: "fake-token",
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _: None,
        **kwargs,
    )


def route_create_event(transport: SpyTransport) -> None:
    transport.route("GET", "/me/calendarView", CALENDAR_VIEW_BEFORE)
    transport.route("POST", "/me/events", EVENT_CREATED)
    transport.route("GET", "/me/events/event-1", EVENT_AFTER)


def route_cancel_event(transport: SpyTransport) -> None:
    transport.route("GET", "/me/events/event-1", EVENT_CREATED)
    transport.route("DELETE", "/me/events/event-1", HttpResponse(status_code=204))


# ── Happy path ────────────────────────────────────────────────────────────────


class TestCalendarEventsHappyPath:
    def test_create_event_returns_created_resource(self, transport: SpyTransport, log_dir: Path) -> None:
        route_create_event(transport)
        result = calendar_client(transport, log_dir).create_event(
            subject="Meeting with Alice",
            start=START,
            end=END,
            attendees=ATTENDEES,
            trigger=TRIGGER,
        )
        assert result == EVENT_CREATED

    def test_create_event_writes_two_audit_lines(self, transport: SpyTransport, log_dir: Path) -> None:
        route_create_event(transport)
        calendar_client(transport, log_dir).create_event(
            "M", START, END, ATTENDEES, trigger=TRIGGER
        )
        all_entries = entries(log_dir)
        assert len(all_entries) == 2
        assert all_entries[0]["audit_phase"] == "intent"
        assert all_entries[1]["audit_phase"] == "outcome"

    def test_before_is_calendar_view_conflict_check(self, transport: SpyTransport, log_dir: Path) -> None:
        route_create_event(transport)
        calendar_client(transport, log_dir).create_event(
            "M", START, END, ATTENDEES, trigger=TRIGGER
        )
        outcome = outcome_entry(log_dir)
        assert outcome["before"] == CALENDAR_VIEW_BEFORE
        assert outcome["operation"] == "create"
        assert outcome["destination"] == "msgraph_calendar_events"
        assert outcome["actor"] == "msgraph_calendar_events_client"

    def test_after_is_the_read_back_event(self, transport: SpyTransport, log_dir: Path) -> None:
        route_create_event(transport)
        calendar_client(transport, log_dir).create_event(
            "M", START, END, ATTENDEES, trigger=TRIGGER
        )
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == EVENT_AFTER
        assert outcome["record_id"] == "event-1"

    def test_calendar_view_precedes_post(self, transport: SpyTransport, log_dir: Path) -> None:
        route_create_event(transport)
        calendar_client(transport, log_dir).create_event(
            "M", START, END, ATTENDEES, trigger=TRIGGER
        )
        methods = [m for m, _ in transport.paths]
        assert methods[0] == "GET"  # calendarView
        post_idx = next(i for i, m in enumerate(methods) if m == "POST")
        assert post_idx > 0

    def test_create_event_body_includes_attendees_and_body_html(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_create_event(transport)
        calendar_client(transport, log_dir).create_event(
            "M", START, END, ATTENDEES,
            body_html="<p>Join Zoom</p>",
            location_display_name="https://zoom.us/j/123",
            trigger=TRIGGER,
        )
        post_body = next(c.json_body for c in transport.calls if c.method == "POST")
        assert post_body["body"]["content"] == "<p>Join Zoom</p>"
        assert post_body["body"]["contentType"] == "HTML"
        assert post_body["location"]["displayName"] == "https://zoom.us/j/123"

    def test_cancel_event_records_pre_delete_before_and_null_after(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_cancel_event(transport)
        calendar_client(transport, log_dir).cancel_event("event-1", trigger=TRIGGER)
        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "delete"
        assert outcome["before"] == EVENT_CREATED
        assert outcome["after"] is None
        assert outcome["record_id"] == "event-1"


# ── Fetch-after failure ───────────────────────────────────────────────────────


class TestCalendarEventsFetchAfterFailure:
    def test_create_still_logs_and_flags_when_read_back_fails(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        transport.route("GET", "/me/calendarView", CALENDAR_VIEW_BEFORE)
        transport.route("POST", "/me/events", EVENT_CREATED)
        transport.route("GET", "/me/events/event-1", HttpResponse(status_code=500, body=b"oops"))
        result = calendar_client(transport, log_dir, max_retries=0).create_event(
            "M", START, END, ATTENDEES, trigger=TRIGGER
        )
        assert result == EVENT_CREATED
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after_fetch_failed"] is True


# ── Gate tests ────────────────────────────────────────────────────────────────


class TestLogFailureBlocksCalendarEventWrite:
    def test_append_raising_blocks_destination_post(
        self, transport: SpyTransport, log_dir: Path, monkeypatch
    ) -> None:
        route_create_event(transport)

        def exploding_append(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(write_audit_log, "append_entry", exploding_append)

        with pytest.raises(WriteAuditLogError) as excinfo:
            calendar_client(transport, log_dir).create_event(
                "M", START, END, ATTENDEES, trigger=TRIGGER
            )

        assert transport.writes == []
        assert "NOT attempted" in str(excinfo.value)
        assert TRIGGER in str(excinfo.value)

    def test_unwritable_log_dir_blocks_post(
        self, transport: SpyTransport, tmp_path: Path
    ) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")

        route_create_event(transport)
        read_only = tmp_path / "readonly"
        read_only.mkdir()
        os.chmod(read_only, 0o500)
        try:
            with pytest.raises(WriteAuditLogError):
                calendar_client(transport, read_only).create_event(
                    "M", START, END, ATTENDEES, trigger=TRIGGER
                )
        finally:
            os.chmod(read_only, 0o700)

        assert transport.writes == []

    def test_cancel_blocks_delete_on_audit_failure(
        self, transport: SpyTransport, log_dir: Path, monkeypatch
    ) -> None:
        route_cancel_event(transport)

        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )

        with pytest.raises(WriteAuditLogError):
            calendar_client(transport, log_dir).cancel_event("event-1", trigger=TRIGGER)

        assert transport.writes == []


# ── Trigger enforcement ───────────────────────────────────────────────────────


class TestCalendarEventsTriggerRequired:
    def test_missing_trigger_blocks_before_any_http(
        self, transport: SpyTransport, log_dir: Path
    ) -> None:
        route_create_event(transport)
        route_cancel_event(transport)
        calls = [
            lambda: calendar_client(transport, log_dir).create_event("M", START, END, ATTENDEES, trigger=""),
            lambda: calendar_client(transport, log_dir).create_event("M", START, END, ATTENDEES, trigger="  "),
            lambda: calendar_client(transport, log_dir).cancel_event("event-1", trigger=""),
        ]
        for call in calls:
            with pytest.raises(MissingWriteTriggerError):
                call()
        assert transport.calls == []
        assert entries(log_dir) == []
