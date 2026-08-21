"""Tests for the audited GoHighLevel write client.

Same shape as the Graph client tests, and for the same reason: the important
assertion is ``transport.writes == []`` when the audit append fails, for every
write operation that replaces an MCP tool call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tools.write_audit_log as write_audit_log
from tests.fakes.write_audit_http import SpyTransport
from tools.ghl_client import (
    GHL_ACCOUNT_KEYS,
    GHL_API_BASE_URL,
    GHL_API_VERSION,
    GoHighLevelWriteClient,
    api_key_env_var,
    api_key_from_hermes_env,
)
from tools.sync_json_http import HttpResponse
from tools.write_audit_log import (
    AFTER_UNKNOWN,
    MissingWriteTriggerError,
    WriteAuditLogError,
    iter_entries,
)

LOCATION_ID = "loc-1"
TRIGGER = "Nathan intro triage 2026-08-21"

CONTACT_BEFORE = {"id": "g-1", "firstName": "Nathan", "tags": ["lead"]}
CONTACT_AFTER = {"id": "g-1", "firstName": "Nathan", "tags": ["lead", "intro"]}
CONTACT_CREATED = {"id": "g-9", "firstName": "Fresh"}


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
    return SpyTransport(GHL_API_BASE_URL)


def client(transport: SpyTransport, log_dir: Path, **kwargs) -> GoHighLevelWriteClient:
    return GoHighLevelWriteClient(
        "team_duncan",
        api_key="fake-pit",
        location_id=LOCATION_ID,
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _seconds: None,
        **kwargs,
    )


# ── Route helpers ────────────────────────────────────────────────────────────


def route_create(transport: SpyTransport) -> None:
    transport.route("POST", "/contacts/", {"contact": CONTACT_CREATED})
    transport.route("GET", "/contacts/g-9", {"contact": CONTACT_CREATED})


def route_update(transport: SpyTransport) -> None:
    transport.route(
        "GET", "/contacts/g-1", {"contact": CONTACT_BEFORE}, {"contact": CONTACT_AFTER}
    )
    transport.route("PUT", "/contacts/g-1", {"contact": CONTACT_AFTER})


def route_delete(transport: SpyTransport) -> None:
    transport.route("GET", "/contacts/g-1", {"contact": CONTACT_BEFORE})
    transport.route("DELETE", "/contacts/g-1", {"succeded": True})


def route_upsert_existing(transport: SpyTransport) -> None:
    transport.route("GET", "/contacts/search/duplicate", {"contact": CONTACT_BEFORE})
    transport.route("POST", "/contacts/upsert", {"new": False, "contact": CONTACT_AFTER})
    transport.route("GET", "/contacts/g-1", {"contact": CONTACT_AFTER})


def route_upsert_new(transport: SpyTransport) -> None:
    transport.route("GET", "/contacts/search/duplicate", {"contact": None})
    transport.route("POST", "/contacts/upsert", {"new": True, "contact": CONTACT_CREATED})
    transport.route("GET", "/contacts/g-9", {"contact": CONTACT_CREATED})


def route_add_tags(transport: SpyTransport) -> None:
    transport.route(
        "GET", "/contacts/g-1", {"contact": CONTACT_BEFORE}, {"contact": CONTACT_AFTER}
    )
    transport.route("POST", "/contacts/g-1/tags", {"tags": ["lead", "intro"]})


def route_remove_tags(transport: SpyTransport) -> None:
    transport.route(
        "GET", "/contacts/g-1", {"contact": CONTACT_AFTER}, {"contact": CONTACT_BEFORE}
    )
    transport.route("DELETE", "/contacts/g-1/tags", {"tags": ["lead"]})


UPSERT_BODY = {"email": "nathan@example.com", "firstName": "Nathan"}

WRITE_OPERATIONS = [
    (
        "create_contact",
        route_create,
        lambda c, t: client(t, c).create_contact({"firstName": "Fresh"}, trigger=TRIGGER),
    ),
    (
        "update_contact",
        route_update,
        lambda c, t: client(t, c).update_contact("g-1", {"firstName": "Nate"}, trigger=TRIGGER),
    ),
    (
        "delete_contact",
        route_delete,
        lambda c, t: client(t, c).delete_contact("g-1", trigger=TRIGGER),
    ),
    (
        "upsert_contact_existing",
        route_upsert_existing,
        lambda c, t: client(t, c).upsert_contact(UPSERT_BODY, trigger=TRIGGER),
    ),
    (
        "upsert_contact_new",
        route_upsert_new,
        lambda c, t: client(t, c).upsert_contact(UPSERT_BODY, trigger=TRIGGER),
    ),
    (
        "add_tags",
        route_add_tags,
        lambda c, t: client(t, c).add_tags("g-1", ["intro"], trigger=TRIGGER),
    ),
    (
        "remove_tags",
        route_remove_tags,
        lambda c, t: client(t, c).remove_tags("g-1", ["intro"], trigger=TRIGGER),
    ),
]

OPERATION_IDS = [case[0] for case in WRITE_OPERATIONS]


# ── The test that matters ────────────────────────────────────────────────────


class TestLogFailureBlocksTheWriteEntirely:
    @pytest.mark.parametrize("_name,setup_routes,call", WRITE_OPERATIONS, ids=OPERATION_IDS)
    def test_append_raising_blocks_the_destination_call(
        self, _name, setup_routes, call, transport, log_dir, monkeypatch
    ) -> None:
        setup_routes(transport)
        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )

        with pytest.raises(WriteAuditLogError) as excinfo:
            call(log_dir, transport)

        assert transport.writes == [], (
            f"GoHighLevel was called despite the audit append failing: {transport.writes}"
        )
        message = str(excinfo.value)
        assert "ghl_contacts" in message
        assert "NOT attempted" in message
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
        setup_routes(transport)
        transport.force_mutating_responses(HttpResponse(status_code=429))
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


# ── Happy path ───────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_create(self, transport, log_dir) -> None:
        route_create(transport)
        created = client(transport, log_dir).create_contact(
            {"firstName": "Fresh"}, trigger=TRIGGER
        )
        assert created == CONTACT_CREATED

        outcome = outcome_entry(log_dir)
        assert outcome["destination"] == "ghl_contacts"
        assert outcome["actor"] == "ghl_client"
        assert outcome["operation"] == "create"
        assert outcome["before"] is None
        assert outcome["after"] == CONTACT_CREATED
        assert outcome["record_id"] == "g-9"
        assert outcome["trigger"] == TRIGGER

    def test_create_injects_the_location_id(self, transport, log_dir) -> None:
        route_create(transport)
        client(transport, log_dir).create_contact({"firstName": "Fresh"}, trigger=TRIGGER)
        post = transport.writes[0]
        assert post.json_body["locationId"] == LOCATION_ID

    def test_update(self, transport, log_dir) -> None:
        route_update(transport)
        result = client(transport, log_dir).update_contact(
            "g-1", {"firstName": "Nate"}, trigger=TRIGGER
        )
        assert result == CONTACT_AFTER

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "update"
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["after"] == CONTACT_AFTER

    def test_delete(self, transport, log_dir) -> None:
        route_delete(transport)
        returned = client(transport, log_dir).delete_contact("g-1", trigger=TRIGGER)
        assert returned == CONTACT_BEFORE

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "delete"
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["after"] is None

    def test_upsert_of_an_existing_contact_is_logged_as_an_update(
        self, transport, log_dir
    ) -> None:
        route_upsert_existing(transport)
        client(transport, log_dir).upsert_contact(UPSERT_BODY, trigger=TRIGGER)

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "update"
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["after"] == CONTACT_AFTER
        assert outcome["record_id"] == "g-1"

    def test_upsert_of_an_unknown_contact_is_logged_as_a_create(
        self, transport, log_dir
    ) -> None:
        route_upsert_new(transport)
        client(transport, log_dir).upsert_contact(UPSERT_BODY, trigger=TRIGGER)

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "create"
        assert outcome["before"] is None
        assert outcome["after"] == CONTACT_CREATED
        assert outcome["record_id"] == "g-9"

    def test_upsert_without_id_email_or_phone_is_refused_before_anything_happens(
        self, transport, log_dir
    ) -> None:
        with pytest.raises(ValueError, match="id, email, or phone"):
            client(transport, log_dir).upsert_contact({"firstName": "Nathan"}, trigger=TRIGGER)
        assert transport.writes == []
        assert entries(log_dir) == []

    def test_add_tags_is_logged_as_an_update_to_the_contact(self, transport, log_dir) -> None:
        route_add_tags(transport)
        client(transport, log_dir).add_tags("g-1", ["intro"], trigger=TRIGGER)

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "update"
        assert outcome["record_id"] == "g-1"
        assert outcome["before"]["tags"] == ["lead"]
        assert outcome["after"]["tags"] == ["lead", "intro"]

    def test_remove_tags_is_logged_as_an_update_to_the_contact(
        self, transport, log_dir
    ) -> None:
        route_remove_tags(transport)
        client(transport, log_dir).remove_tags("g-1", ["intro"], trigger=TRIGGER)

        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "update"
        assert outcome["before"]["tags"] == ["lead", "intro"]
        assert outcome["after"]["tags"] == ["lead"]

    def test_empty_tag_list_is_refused_before_the_gate(self, transport, log_dir) -> None:
        with pytest.raises(ValueError, match="at least one tag"):
            client(transport, log_dir).add_tags("g-1", [], trigger=TRIGGER)
        assert transport.calls == []
        assert entries(log_dir) == []


# ── Fetch-after failure ──────────────────────────────────────────────────────


class TestFetchAfterFailure:
    def test_create_read_back_failure_is_flagged_not_raised(self, transport, log_dir) -> None:
        transport.route("POST", "/contacts/", {"contact": CONTACT_CREATED})
        transport.route("GET", "/contacts/g-9", HttpResponse(status_code=500, body=b"boom"))

        created = client(transport, log_dir, max_retries=0).create_contact(
            {"firstName": "Fresh"}, trigger=TRIGGER
        )

        assert created == CONTACT_CREATED
        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after"] == "UNKNOWN — verify manually"
        assert outcome["after_fetch_failed"] is True

    def test_tag_write_read_back_failure_is_flagged(self, transport, log_dir) -> None:
        transport.route(
            "GET", "/contacts/g-1", {"contact": CONTACT_BEFORE}, HttpResponse(status_code=503)
        )
        transport.route("POST", "/contacts/g-1/tags", {"tags": ["lead", "intro"]})

        client(transport, log_dir, max_retries=0).add_tags("g-1", ["intro"], trigger=TRIGGER)

        outcome = outcome_entry(log_dir)
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after_fetch_failed"] is True
        assert outcome["before"] == CONTACT_BEFORE


# ── Trigger enforcement ──────────────────────────────────────────────────────


class TestTriggerIsRequired:
    def test_every_write_entrypoint_refuses_a_blank_trigger(self, transport, log_dir) -> None:
        ghl = client(transport, log_dir)
        calls = [
            lambda: ghl.create_contact({"firstName": "x"}, trigger=""),
            lambda: ghl.update_contact("g-1", {"firstName": "x"}, trigger="  "),
            lambda: ghl.delete_contact("g-1", trigger=""),
            lambda: ghl.upsert_contact(UPSERT_BODY, trigger=""),
            lambda: ghl.add_tags("g-1", ["intro"], trigger="   "),
            lambda: ghl.remove_tags("g-1", ["intro"], trigger=""),
        ]
        for call in calls:
            with pytest.raises(MissingWriteTriggerError):
                call()

        assert transport.calls == []
        assert entries(log_dir) == []


# ── Credentials and headers ──────────────────────────────────────────────────


class TestCredentials:
    def test_env_var_names_match_the_mcp_server_convention(self) -> None:
        assert api_key_env_var("team_duncan") == "MCP_GHL_TEAM_DUNCAN_API_KEY"
        assert set(GHL_ACCOUNT_KEYS) == {
            "chillcabins",
            "clay_personal",
            "team_duncan",
            "tracey",
        }

    def test_key_is_read_from_the_hermes_dotenv(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            "# comment\nMCP_GHL_TRACEY_API_KEY=pit-abc123\n", encoding="utf-8"
        )
        assert api_key_from_hermes_env("tracey", hermes_home=tmp_path) == "pit-abc123"

    def test_missing_key_names_the_variable_and_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as excinfo:
            api_key_from_hermes_env("chillcabins", hermes_home=tmp_path)
        assert "MCP_GHL_CHILLCABINS_API_KEY" in str(excinfo.value)

    def test_requests_carry_the_bearer_token_and_version_header(
        self, transport, log_dir
    ) -> None:
        route_create(transport)
        client(transport, log_dir).create_contact({"firstName": "x"}, trigger=TRIGGER)
        headers = transport.calls[0].headers
        assert headers["Authorization"] == "Bearer fake-pit"
        assert headers["Version"] == GHL_API_VERSION
        assert headers["User-Agent"]
