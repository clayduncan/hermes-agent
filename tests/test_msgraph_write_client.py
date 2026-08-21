"""Tests for the audited Microsoft Graph contact/task write clients.

The load-bearing class here is :class:`TestLogFailureBlocksTheWriteEntirely`.
Everything else confirms the log is *correct*; that class confirms the log is a
*precondition* — that no write reaches Graph when the audit append fails, on the
first attempt, not merely the last.
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
    MicrosoftGraphContactsWriteClient,
    MicrosoftGraphTasksWriteClient,
)
from tools.sync_json_http import HttpResponse
from tools.write_audit_log import (
    AFTER_UNKNOWN,
    MissingWriteTriggerError,
    WriteAuditLogError,
    iter_entries,
)

LIST_ID = "list-1"
TRIGGER = "Nathan intro triage 2026-08-21"

CONTACT_BEFORE = {"id": "c-1", "displayName": "Nathan Old", "companyName": "Acme"}
CONTACT_AFTER = {"id": "c-1", "displayName": "Nathan New", "companyName": "Acme"}
CONTACT_CREATED = {"id": "c-9", "displayName": "Fresh Contact"}

TASK_BEFORE = {"id": "t-1", "title": "Old title", "status": "notStarted"}
TASK_AFTER = {"id": "t-1", "title": "New title", "status": "notStarted"}
TASK_CREATED = {"id": "t-9", "title": "Fresh task"}


def entries(log_dir: Path) -> list[dict]:
    return [entry for _, _, entry in iter_entries(log_dir)]


def outcome_entry(log_dir: Path) -> dict:
    found = [entry for entry in entries(log_dir) if entry["audit_phase"] == "outcome"]
    assert len(found) == 1, f"expected exactly one outcome entry, got {found}"
    return found[0]


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "write_audit"


@pytest.fixture
def transport() -> SpyTransport:
    return SpyTransport(DEFAULT_GRAPH_BASE_URL)


def contacts_client(transport: SpyTransport, log_dir: Path, **kwargs) -> MicrosoftGraphContactsWriteClient:
    return MicrosoftGraphContactsWriteClient(
        lambda: "fake-token",
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def tasks_client(transport: SpyTransport, log_dir: Path, **kwargs) -> MicrosoftGraphTasksWriteClient:
    return MicrosoftGraphTasksWriteClient(
        lambda: "fake-token",
        list_id=LIST_ID,
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _seconds: None,
        **kwargs,
    )


# ── Route helpers, one per operation under test ──────────────────────────────


def route_contact_create(transport: SpyTransport) -> None:
    transport.route("POST", "/me/contacts", CONTACT_CREATED)
    transport.route("GET", "/me/contacts/c-9", CONTACT_CREATED)


def route_contact_update(transport: SpyTransport) -> None:
    transport.route("GET", "/me/contacts/c-1", CONTACT_BEFORE, CONTACT_AFTER)
    transport.route("PATCH", "/me/contacts/c-1", CONTACT_AFTER)


def route_contact_delete(transport: SpyTransport) -> None:
    transport.route("GET", "/me/contacts/c-1", CONTACT_BEFORE)
    transport.route("DELETE", "/me/contacts/c-1", HttpResponse(status_code=204))


def route_task_create(transport: SpyTransport) -> None:
    transport.route("POST", f"/me/todo/lists/{LIST_ID}/tasks", TASK_CREATED)
    transport.route("GET", f"/me/todo/lists/{LIST_ID}/tasks/t-9", TASK_CREATED)


def route_task_update(transport: SpyTransport) -> None:
    transport.route("GET", f"/me/todo/lists/{LIST_ID}/tasks/t-1", TASK_BEFORE, TASK_AFTER)
    transport.route("PATCH", f"/me/todo/lists/{LIST_ID}/tasks/t-1", TASK_AFTER)


def route_task_delete(transport: SpyTransport) -> None:
    transport.route("GET", f"/me/todo/lists/{LIST_ID}/tasks/t-1", TASK_BEFORE)
    transport.route("DELETE", f"/me/todo/lists/{LIST_ID}/tasks/t-1", HttpResponse(status_code=204))


#: ``(id, route setup, call)`` for every write operation the module exposes.
WRITE_OPERATIONS = [
    (
        "contacts_create",
        route_contact_create,
        lambda c, t: contacts_client(t, c).create_contact({"displayName": "Fresh"}, trigger=TRIGGER),
    ),
    (
        "contacts_update",
        route_contact_update,
        lambda c, t: contacts_client(t, c).update_contact(
            "c-1", {"displayName": "Nathan New"}, trigger=TRIGGER
        ),
    ),
    (
        "contacts_delete",
        route_contact_delete,
        lambda c, t: contacts_client(t, c).delete_contact("c-1", trigger=TRIGGER),
    ),
    (
        "tasks_create",
        route_task_create,
        lambda c, t: tasks_client(t, c).create_task({"title": "Fresh task"}, trigger=TRIGGER),
    ),
    (
        "tasks_update",
        route_task_update,
        lambda c, t: tasks_client(t, c).update_task("t-1", {"title": "New title"}, trigger=TRIGGER),
    ),
    (
        "tasks_delete",
        route_task_delete,
        lambda c, t: tasks_client(t, c).delete_task("t-1", trigger=TRIGGER),
    ),
]

OPERATION_IDS = [case[0] for case in WRITE_OPERATIONS]


# ── The test that matters ────────────────────────────────────────────────────


class TestLogFailureBlocksTheWriteEntirely:
    """For every write operation: a failed audit append means Graph is never called."""

    @pytest.mark.parametrize("_name,setup_routes,call", WRITE_OPERATIONS, ids=OPERATION_IDS)
    def test_append_raising_blocks_the_destination_call(
        self, _name, setup_routes, call, transport, log_dir, monkeypatch
    ) -> None:
        setup_routes(transport)

        def exploding_append(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(write_audit_log, "append_entry", exploding_append)

        with pytest.raises(WriteAuditLogError) as excinfo:
            call(log_dir, transport)

        assert transport.writes == [], (
            "the destination API was invoked despite the audit append failing: "
            f"{transport.writes}"
        )
        message = str(excinfo.value)
        assert "NOT attempted" in message
        assert "No space left on device" in message
        assert TRIGGER in message

    @pytest.mark.parametrize("_name,setup_routes,call", WRITE_OPERATIONS, ids=OPERATION_IDS)
    def test_unwritable_log_directory_blocks_the_destination_call(
        self, _name, setup_routes, call, transport, tmp_path: Path
    ) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")

        setup_routes(transport)
        read_only = tmp_path / "readonly"
        read_only.mkdir()
        os.chmod(read_only, 0o500)
        try:
            with pytest.raises(WriteAuditLogError):
                call(read_only, transport)
        finally:
            os.chmod(read_only, 0o700)

        assert transport.writes == []

    @pytest.mark.parametrize("_name,setup_routes,call", WRITE_OPERATIONS, ids=OPERATION_IDS)
    def test_log_failure_blocks_the_first_attempt_not_just_the_last(
        self, _name, setup_routes, call, transport, log_dir, monkeypatch
    ) -> None:
        """The retry loop lives below the gate, so retries never begin either.

        Every mutating route is wired to return a retryable 503 here: if the gate
        leaked even one attempt through, the spy would record it (and the client
        would burn its whole retry budget doing so).
        """
        setup_routes(transport)
        transport.force_mutating_responses(HttpResponse(status_code=503))

        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("audit log unavailable")),
        )

        with pytest.raises(WriteAuditLogError):
            call(log_dir, transport)

        assert transport.writes == []

    @pytest.mark.parametrize("_name,setup_routes,call", WRITE_OPERATIONS, ids=OPERATION_IDS)
    def test_a_blocked_write_leaves_no_partial_log_entry(
        self, _name, setup_routes, call, transport, log_dir, monkeypatch
    ) -> None:
        setup_routes(transport)
        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        with pytest.raises(WriteAuditLogError):
            call(log_dir, transport)
        assert entries(log_dir) == []


# ── Happy path: before/after correctness ─────────────────────────────────────


class TestContactsHappyPath:
    def test_create_logs_null_before_and_the_created_state_as_after(
        self, transport, log_dir
    ) -> None:
        route_contact_create(transport)
        created = contacts_client(transport, log_dir).create_contact(
            {"displayName": "Fresh Contact"}, trigger=TRIGGER
        )

        assert created == CONTACT_CREATED
        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "create"
        assert outcome["before"] is None
        assert outcome["after"] == CONTACT_CREATED
        assert outcome["record_id"] == "c-9"
        assert outcome["destination"] == "msgraph_contacts"
        assert outcome["actor"] == "msgraph_contacts_client"
        assert outcome["trigger"] == TRIGGER

        intent = entries(log_dir)[0]
        assert intent["audit_phase"] == "intent"
        assert intent["record_id"] is None

    def test_update_logs_a_real_pre_write_fetch_as_before(self, transport, log_dir) -> None:
        route_contact_update(transport)
        result = contacts_client(transport, log_dir).update_contact(
            "c-1", {"displayName": "Nathan New"}, trigger=TRIGGER
        )

        assert result == CONTACT_AFTER
        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "update"
        # `before` is the fetched record, not the PATCH payload reconstructed.
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["before"]["companyName"] == "Acme"
        assert outcome["after"] == CONTACT_AFTER
        assert outcome["record_id"] == "c-1"

    def test_the_before_fetch_precedes_the_audit_line_which_precedes_the_patch(
        self, transport, log_dir
    ) -> None:
        route_contact_update(transport)
        contacts_client(transport, log_dir).update_contact(
            "c-1", {"displayName": "Nathan New"}, trigger=TRIGGER
        )
        # Only reads happen before the gate; the first mutating call is the PATCH.
        assert [method for method, _ in transport.paths] == ["GET", "PATCH", "GET"]

    def test_delete_logs_pre_delete_state_and_null_after(self, transport, log_dir) -> None:
        route_contact_delete(transport)
        returned = contacts_client(transport, log_dir).delete_contact("c-1", trigger=TRIGGER)

        assert returned == CONTACT_BEFORE
        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "delete"
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["after"] is None
        assert outcome["record_id"] == "c-1"


class TestTasksHappyPath:
    def test_create_update_delete_before_after(self, log_dir) -> None:
        create_transport = SpyTransport(DEFAULT_GRAPH_BASE_URL)
        route_task_create(create_transport)
        tasks_client(create_transport, log_dir).create_task({"title": "Fresh task"}, trigger=TRIGGER)
        created = outcome_entry(log_dir)
        assert (created["operation"], created["before"], created["after"]) == (
            "create",
            None,
            TASK_CREATED,
        )
        assert created["destination"] == "msgraph_tasks"
        assert created["actor"] == "msgraph_tasks_client"

    def test_update_before_is_the_pre_write_task(self, transport, log_dir) -> None:
        route_task_update(transport)
        tasks_client(transport, log_dir).update_task("t-1", {"title": "New title"}, trigger=TRIGGER)
        outcome = outcome_entry(log_dir)
        assert outcome["before"] == TASK_BEFORE
        assert outcome["after"] == TASK_AFTER

    def test_delete_after_is_null(self, transport, log_dir) -> None:
        route_task_delete(transport)
        tasks_client(transport, log_dir).delete_task("t-1", trigger=TRIGGER)
        outcome = outcome_entry(log_dir)
        assert outcome["before"] == TASK_BEFORE
        assert outcome["after"] is None

    def test_a_tasks_client_needs_a_list_id(self) -> None:
        with pytest.raises(ValueError, match="list_id"):
            MicrosoftGraphTasksWriteClient(lambda: "t", list_id="")


# ── Fetch-after failure ──────────────────────────────────────────────────────


class TestFetchAfterFailure:
    def test_create_still_logs_and_flags_the_entry_when_read_back_fails(
        self, transport, log_dir
    ) -> None:
        transport.route("POST", "/me/contacts", CONTACT_CREATED)
        transport.route("GET", "/me/contacts/c-9", HttpResponse(status_code=500, body=b"boom"))

        created = contacts_client(transport, log_dir, max_retries=0).create_contact(
            {"displayName": "Fresh Contact"}, trigger=TRIGGER
        )

        # No exception: the write itself succeeded.
        assert created == CONTACT_CREATED
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after"] == "UNKNOWN — verify manually"
        assert outcome["after_fetch_failed"] is True
        assert outcome["record_id"] == "c-9"
        assert outcome["operation"] == "create"

    def test_update_read_back_failure_is_flagged_not_raised(self, transport, log_dir) -> None:
        transport.route("GET", "/me/contacts/c-1", CONTACT_BEFORE, HttpResponse(status_code=503))
        transport.route("PATCH", "/me/contacts/c-1", CONTACT_AFTER)

        result = contacts_client(transport, log_dir, max_retries=0).update_contact(
            "c-1", {"displayName": "Nathan New"}, trigger=TRIGGER
        )

        assert result == CONTACT_AFTER  # falls back to the PATCH response body
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after_fetch_failed"] is True
        assert outcome["before"] == CONTACT_BEFORE

    def test_task_create_read_back_failure_is_flagged(self, transport, log_dir) -> None:
        transport.route("POST", f"/me/todo/lists/{LIST_ID}/tasks", TASK_CREATED)
        transport.route(
            "GET", f"/me/todo/lists/{LIST_ID}/tasks/t-9", HttpResponse(status_code=500)
        )
        tasks_client(transport, log_dir, max_retries=0).create_task(
            {"title": "Fresh task"}, trigger=TRIGGER
        )
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after_fetch_failed"] is True


# ── Trigger enforcement at the client boundary ───────────────────────────────


class TestTriggerIsRequiredAtEveryEntrypoint:
    def test_no_trigger_means_no_log_line_and_no_http_call_at_all(
        self, transport, log_dir
    ) -> None:
        contacts = contacts_client(transport, log_dir)
        tasks = tasks_client(transport, log_dir)
        calls = [
            lambda: contacts.create_contact({"displayName": "x"}, trigger=""),
            lambda: contacts.update_contact("c-1", {"displayName": "x"}, trigger="  "),
            lambda: contacts.delete_contact("c-1", trigger=""),
            lambda: tasks.create_task({"title": "x"}, trigger=""),
            lambda: tasks.update_task("t-1", {"title": "x"}, trigger="   "),
            lambda: tasks.delete_task("t-1", trigger=""),
        ]
        for call in calls:
            with pytest.raises(MissingWriteTriggerError):
                call()

        # Not even the `before` read happened.
        assert transport.calls == []
        assert entries(log_dir) == []


# ── Transport wiring ─────────────────────────────────────────────────────────


class TestTransportWiring:
    def test_requests_carry_the_bearer_token(self, transport, log_dir) -> None:
        route_contact_create(transport)
        contacts_client(transport, log_dir).create_contact({"displayName": "x"}, trigger=TRIGGER)
        assert transport.calls[0].headers["Authorization"] == "Bearer fake-token"

    def test_a_retryable_status_is_retried_then_surfaces_as_an_error(
        self, transport, log_dir
    ) -> None:
        transport.route(
            "POST",
            "/me/contacts",
            HttpResponse(status_code=503),
            HttpResponse(status_code=503),
            HttpResponse(status_code=503),
        )
        from tools.sync_json_http import HttpRequestError

        with pytest.raises(HttpRequestError):
            contacts_client(transport, log_dir, max_retries=2).create_contact(
                {"displayName": "x"}, trigger=TRIGGER
            )
        assert len(transport.writes) == 3
        # The intent line landed first and stays on disk as the record of the attempt.
        logged = entries(log_dir)
        assert len(logged) == 1
        assert logged[0]["audit_phase"] == "intent"

    def test_log_lines_are_one_json_object_each_on_disk(self, transport, log_dir) -> None:
        route_contact_create(transport)
        contacts_client(transport, log_dir).create_contact({"displayName": "x"}, trigger=TRIGGER)
        month_file = next(iter(sorted(log_dir.glob("*.jsonl"))))
        lines = month_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(isinstance(json.loads(line), dict) for line in lines)


def test_clock_injection_places_entries_in_the_right_month(transport, log_dir) -> None:
    route_contact_create(transport)
    july = datetime(2026, 7, 15, 9, 0, 0, tzinfo=timezone.utc)
    contacts_client(transport, log_dir, clock=lambda: july).create_contact(
        {"displayName": "x"}, trigger=TRIGGER
    )
    assert (log_dir / "2026-07.jsonl").exists()
