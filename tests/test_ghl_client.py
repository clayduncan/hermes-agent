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
    FIXED_SCOPE_BINDINGS,
    GHL_ACCOUNT_KEYS,
    GHL_API_BASE_URL,
    GHL_API_VERSION,
    ScopeViolationError,
    TEAM_DUNCAN_ACCOUNT_KEY,
    TEAM_DUNCAN_LOCATION_ID,
    GoHighLevelWriteClient,
    api_key_env_var,
    api_key_from_hermes_env,
    scoped_client,
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


NOTE_BODY = "Incoming call · Answered · 2 min 12 sec"
NOTE_CREATED = {"id": "note-1", "contactId": "g-1", "body": NOTE_BODY}
NOTE_UPDATED_BODY = "Incoming call · Missed · 0 sec"
NOTE_BEFORE = {"id": "note-1", "contactId": "g-1", "body": NOTE_BODY}
NOTE_AFTER = {"id": "note-1", "contactId": "g-1", "body": NOTE_UPDATED_BODY}

CALL_NOTE_COLOR = "#D9EAD3"

#: A before-state carrying every optional note field, used to prove
#: update_note() preserves what the caller doesn't explicitly replace.
NOTE_BEFORE_FULL = {
    "id": "note-1",
    "contactId": "g-1",
    "body": NOTE_BODY,
    "userId": "user-123",
    "title": "Call log",
    "pinned": True,
    "color": "#FFFFFF",
}
NOTE_AFTER_GREEN = {**NOTE_BEFORE_FULL, "body": NOTE_UPDATED_BODY, "color": CALL_NOTE_COLOR}


def route_update_note(transport: SpyTransport, contact_id: str = "g-1") -> None:
    in_scope = {**CONTACT_BEFORE, "id": contact_id, "locationId": LOCATION_ID}
    transport.route("GET", f"/contacts/{contact_id}", {"contact": in_scope})
    transport.route(
        "GET", f"/contacts/{contact_id}/notes/note-1", {"note": NOTE_BEFORE}, {"note": NOTE_AFTER}
    )
    transport.route("PUT", f"/contacts/{contact_id}/notes/note-1", {"note": NOTE_AFTER})


def route_update_note_full(transport: SpyTransport, contact_id: str = "g-1") -> None:
    """Same as route_update_note(), but the before-state carries userId,
    title, pinned, and color -- used to prove update_note() preserves them."""
    in_scope = {**CONTACT_BEFORE, "id": contact_id, "locationId": LOCATION_ID}
    transport.route("GET", f"/contacts/{contact_id}", {"contact": in_scope})
    transport.route(
        "GET", f"/contacts/{contact_id}/notes/note-1",
        {"note": NOTE_BEFORE_FULL}, {"note": NOTE_AFTER_GREEN},
    )
    transport.route("PUT", f"/contacts/{contact_id}/notes/note-1", {"note": NOTE_AFTER_GREEN})


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
    (
        "update_note",
        route_update_note,
        lambda c, t: client(t, c).update_note("g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER),
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


# ── Scoped-client construction boundary (OPS-104 acceptance A, E, F) ─────────


class TestScopedClientConstruction:
    """scoped_client() is the one enforcement point for account/location pairing."""

    def test_wrong_location_for_team_duncan_account_fails_closed(
        self, transport, log_dir
    ) -> None:
        with pytest.raises(ScopeViolationError, match="pinned to location"):
            scoped_client(
                TEAM_DUNCAN_ACCOUNT_KEY,
                "not-the-real-team-duncan-location",
                api_key="fake-pit",
                request_fn=transport,
                log_dir=log_dir,
            )
        assert transport.calls == []
        assert entries(log_dir) == []

    def test_wrong_account_key_for_the_fixed_location_fails_closed(
        self, transport, log_dir
    ) -> None:
        with pytest.raises(ScopeViolationError, match="pinned to account"):
            scoped_client(
                "tracey",
                TEAM_DUNCAN_LOCATION_ID,
                api_key="fake-pit",
                request_fn=transport,
                log_dir=log_dir,
            )
        assert transport.calls == []
        assert entries(log_dir) == []

    def test_correct_team_duncan_pairing_succeeds(self, transport, log_dir) -> None:
        route_create(transport)
        ghl = scoped_client(
            TEAM_DUNCAN_ACCOUNT_KEY,
            TEAM_DUNCAN_LOCATION_ID,
            api_key="fake-pit",
            request_fn=transport,
            log_dir=log_dir,
            sleep=lambda _seconds: None,
        )
        created = ghl.create_contact({"firstName": "Fresh"}, trigger=TRIGGER)
        assert created == CONTACT_CREATED

    def test_unrelated_accounts_have_no_fixed_binding(self, transport, log_dir) -> None:
        """Tracey and Chill Cabins aren't pinned: scoped_client() is transparent for them."""
        assert "tracey" not in FIXED_SCOPE_BINDINGS
        assert "chillcabins" not in FIXED_SCOPE_BINDINGS
        route_create(transport)
        ghl = scoped_client(
            "tracey",
            "tracey-own-location",
            api_key="fake-pit",
            request_fn=transport,
            log_dir=log_dir,
            sleep=lambda _seconds: None,
        )
        created = ghl.create_contact({"firstName": "Fresh"}, trigger=TRIGGER)
        assert created == CONTACT_CREATED
        assert transport.writes[0].json_body["locationId"] == "tracey-own-location"


# ── Payload location is fixed to the client's scope (acceptance D) ──────────


class TestPayloadLocationIsEnforced:
    def test_create_with_mismatched_location_fails_before_the_gate(
        self, transport, log_dir
    ) -> None:
        route_create(transport)
        with pytest.raises(ScopeViolationError, match="locationId"):
            client(transport, log_dir).create_contact(
                {"firstName": "Fresh", "locationId": "some-other-location"},
                trigger=TRIGGER,
            )
        assert transport.calls == []
        assert entries(log_dir) == []

    def test_create_with_matching_explicit_location_succeeds(
        self, transport, log_dir
    ) -> None:
        route_create(transport)
        created = client(transport, log_dir).create_contact(
            {"firstName": "Fresh", "locationId": LOCATION_ID}, trigger=TRIGGER
        )
        assert created == CONTACT_CREATED

    def test_upsert_with_mismatched_location_fails_before_the_gate(
        self, transport, log_dir
    ) -> None:
        route_upsert_new(transport)
        with pytest.raises(ScopeViolationError, match="locationId"):
            client(transport, log_dir).upsert_contact(
                {**UPSERT_BODY, "locationId": "some-other-location"}, trigger=TRIGGER
            )
        assert transport.calls == []
        assert entries(log_dir) == []

    def test_upsert_with_matching_explicit_location_succeeds(
        self, transport, log_dir
    ) -> None:
        route_upsert_new(transport)
        client(transport, log_dir).upsert_contact(
            {**UPSERT_BODY, "locationId": LOCATION_ID}, trigger=TRIGGER
        )
        outcome = outcome_entry(log_dir)
        assert outcome["operation"] == "create"


# ── Reads go through the same scope boundary as writes (acceptance B, C) ────


class TestReadsAreScopedToOneLocation:
    def test_find_duplicate_only_sends_the_fixed_location(
        self, transport, log_dir
    ) -> None:
        transport.route("GET", "/contacts/search/duplicate", {"contact": CONTACT_BEFORE})
        client(transport, log_dir).find_duplicate(phone="+15551234567")
        assert f"locationId={LOCATION_ID}" in transport.calls[0].url

    def test_find_duplicate_rejects_a_result_from_another_location(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/search/duplicate", {"contact": foreign})
        assert client(transport, log_dir).find_duplicate(phone="+15551234567") is None

    def test_get_contact_in_scope_returns_none_for_another_location(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        assert client(transport, log_dir).get_contact_in_scope("g-1") is None

    def test_get_contact_in_scope_returns_the_contact_for_the_matching_location(
        self, transport, log_dir
    ) -> None:
        same = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": same})
        assert client(transport, log_dir).get_contact_in_scope("g-1") == same

    def test_search_contacts_sends_the_fixed_location_and_filters_results(
        self, transport, log_dir
    ) -> None:
        transport.route(
            "GET",
            "/contacts/search",
            {
                "contacts": [
                    {"id": "g-1", "locationId": LOCATION_ID, "firstName": "Nathan"},
                    {"id": "g-99", "locationId": "some-other-location", "firstName": "Other"},
                ]
            },
        )
        results = client(transport, log_dir).search_contacts("Nathan")
        assert [c["id"] for c in results] == ["g-1"]
        assert f"locationId={LOCATION_ID}" in transport.calls[0].url


class TestCrossAccountIsolation:
    """Acceptance C: the same phone/email in two sub-accounts never crosses over."""

    def test_same_phone_in_two_sub_accounts_only_resolves_within_its_own(
        self, log_dir
    ) -> None:
        td_transport = SpyTransport(GHL_API_BASE_URL)
        td_transport.route(
            "GET",
            "/contacts/search/duplicate",
            {"contact": {"id": "g-td", "locationId": TEAM_DUNCAN_LOCATION_ID, "firstName": "Nathan"}},
        )
        tracey_transport = SpyTransport(GHL_API_BASE_URL)
        tracey_transport.route(
            "GET",
            "/contacts/search/duplicate",
            {"contact": {"id": "g-tracey", "locationId": "tracey-location", "firstName": "Nathan"}},
        )

        td_client = scoped_client(
            TEAM_DUNCAN_ACCOUNT_KEY,
            TEAM_DUNCAN_LOCATION_ID,
            api_key="fake-pit",
            request_fn=td_transport,
            log_dir=log_dir,
        )
        tracey_client = GoHighLevelWriteClient(
            "tracey",
            api_key="fake-pit",
            location_id="tracey-location",
            request_fn=tracey_transport,
            log_dir=log_dir,
        )

        td_result = td_client.find_duplicate(phone="+15551234567")
        tracey_result = tracey_client.find_duplicate(phone="+15551234567")

        assert td_result["id"] == "g-td"
        assert tracey_result["id"] == "g-tracey"
        assert f"locationId={TEAM_DUNCAN_LOCATION_ID}" in td_transport.calls[0].url
        assert "locationId=tracey-location" in tracey_transport.calls[0].url


# ── Tracey and Chill Cabins keep working through the shared client (acceptance E) ─


class TestExistingSubAccountsStillWork:
    def test_tracey_create_still_works_with_its_own_scope(self, log_dir) -> None:
        transport = SpyTransport(GHL_API_BASE_URL)
        transport.route("POST", "/contacts/", {"contact": CONTACT_CREATED})
        transport.route("GET", "/contacts/g-9", {"contact": CONTACT_CREATED})
        ghl = GoHighLevelWriteClient(
            "tracey",
            api_key="fake-pit",
            location_id="tracey-location",
            request_fn=transport,
            log_dir=log_dir,
            sleep=lambda _seconds: None,
        )
        created = ghl.create_contact({"firstName": "Fresh"}, trigger=TRIGGER)
        assert created == CONTACT_CREATED
        assert transport.writes[0].json_body["locationId"] == "tracey-location"

    def test_chillcabins_create_still_works_with_its_own_scope(self, log_dir) -> None:
        transport = SpyTransport(GHL_API_BASE_URL)
        transport.route("POST", "/contacts/", {"contact": CONTACT_CREATED})
        transport.route("GET", "/contacts/g-9", {"contact": CONTACT_CREATED})
        ghl = GoHighLevelWriteClient(
            "chillcabins",
            api_key="fake-pit",
            location_id="chillcabins-location",
            request_fn=transport,
            log_dir=log_dir,
            sleep=lambda _seconds: None,
        )
        created = ghl.create_contact({"firstName": "Fresh"}, trigger=TRIGGER)
        assert created == CONTACT_CREATED
        assert transport.writes[0].json_body["locationId"] == "chillcabins-location"


# ── OPS-18: contact notes (create/read/update) ────────────────────────────────


def route_create_note(transport: SpyTransport, contact_id: str = "g-1") -> None:
    in_scope = {**CONTACT_BEFORE, "id": contact_id, "locationId": LOCATION_ID}
    transport.route("GET", f"/contacts/{contact_id}", {"contact": in_scope})
    transport.route("POST", f"/contacts/{contact_id}/notes", {"note": NOTE_CREATED})
    transport.route("GET", f"/contacts/{contact_id}/notes/note-1", {"note": NOTE_CREATED})


class TestNoteAuditOrdering:
    """Same invariant as every other write: the audit intent must land before
    the destination is ever called, and a failed append blocks the POST."""

    def test_append_raising_blocks_the_create_note_call(
        self, transport, log_dir, monkeypatch
    ) -> None:
        route_create_note(transport)
        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )
        with pytest.raises(WriteAuditLogError):
            client(transport, log_dir).create_note("g-1", "note body", trigger=TRIGGER)
        assert [c for c in transport.writes if "/notes" in c.url] == []

    def test_scope_check_happens_before_any_audit_line_or_call(
        self, transport, log_dir
    ) -> None:
        """A contact outside this client's scope fails before the audit
        intent is written and before any note endpoint is called."""
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).create_note("g-1", "note body", trigger=TRIGGER)
        assert transport.paths == [("GET", "/contacts/g-1")]  # only the scope-check GET
        assert entries(log_dir) == []

    def test_create_note_reads_before_writing_and_reads_back_after(
        self, transport, log_dir
    ) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note("g-1", NOTE_BODY, trigger=TRIGGER)
        assert transport.paths == [
            ("GET", "/contacts/g-1"),
            ("POST", "/contacts/g-1/notes"),
            ("GET", "/contacts/g-1/notes/note-1"),
        ]
        outcome = outcome_entry(log_dir)
        assert outcome["destination"] == "ghl_contacts"
        assert outcome["operation"] == "create"
        assert outcome["before"] is None
        assert outcome["record_id"] == "note-1"
        assert outcome["after"] == NOTE_CREATED


class TestNoteUpdateAuditOrdering:
    """Same invariant as create_note: the audit intent must land before the
    destination is ever called, contact scope is checked first, and the
    write is followed by an exact read-back."""

    def test_append_raising_blocks_the_update_note_call(
        self, transport, log_dir, monkeypatch
    ) -> None:
        route_update_note(transport)
        monkeypatch.setattr(
            write_audit_log,
            "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )
        with pytest.raises(WriteAuditLogError):
            client(transport, log_dir).update_note("g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER)
        assert [c for c in transport.writes if "/notes" in c.url] == []

    def test_scope_check_happens_before_any_audit_line_or_call(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).update_note("g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER)
        assert transport.paths == [("GET", "/contacts/g-1")]  # only the scope-check GET
        assert entries(log_dir) == []

    def test_update_note_reads_before_writing_and_reads_back_after(
        self, transport, log_dir
    ) -> None:
        route_update_note(transport)
        result = client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER
        )
        assert result == NOTE_AFTER
        assert transport.paths == [
            ("GET", "/contacts/g-1"),  # contact-scope check
            ("GET", "/contacts/g-1/notes/note-1"),  # before-state
            ("PUT", "/contacts/g-1/notes/note-1"),
            ("GET", "/contacts/g-1/notes/note-1"),  # exact read-back
        ]
        outcome = outcome_entry(log_dir)
        assert outcome["destination"] == "ghl_contacts"
        assert outcome["operation"] == "update"
        assert outcome["record_id"] == "note-1"
        assert outcome["before"] == NOTE_BEFORE
        assert outcome["after"] == NOTE_AFTER
        assert outcome["trigger"] == TRIGGER

    def test_empty_body_is_refused_before_the_gate(self, transport, log_dir) -> None:
        in_scope = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": in_scope})
        with pytest.raises(ValueError, match="non-empty body"):
            client(transport, log_dir).update_note("g-1", "note-1", "  ", trigger=TRIGGER)
        assert entries(log_dir) == []
        assert [c for c in transport.writes if "/notes" in c.url] == []


class TestNoteScopeRejection:
    def test_create_note_rejects_a_missing_contact(self, transport, log_dir) -> None:
        transport.route("GET", "/contacts/g-404", HttpResponse(status_code=404))
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).create_note("g-404", "note body", trigger=TRIGGER)
        assert entries(log_dir) == []

    def test_list_notes_rejects_a_contact_from_another_location(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).list_notes("g-1")

    def test_get_note_rejects_a_contact_from_another_location(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).get_note("g-1", "note-1")

    def test_update_note_rejects_a_contact_from_another_location(
        self, transport, log_dir
    ) -> None:
        foreign = {**CONTACT_BEFORE, "locationId": "some-other-location"}
        transport.route("GET", "/contacts/g-1", {"contact": foreign})
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).update_note("g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER)
        assert transport.paths == [("GET", "/contacts/g-1")]
        assert entries(log_dir) == []

    def test_update_note_rejects_a_missing_contact(self, transport, log_dir) -> None:
        transport.route("GET", "/contacts/g-404", HttpResponse(status_code=404))
        with pytest.raises(ScopeViolationError):
            client(transport, log_dir).update_note("g-404", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER)
        assert entries(log_dir) == []


class TestNoteCreateAndReadBack:
    def test_create_note_returns_the_created_note(self, transport, log_dir) -> None:
        route_create_note(transport)
        created = client(transport, log_dir).create_note("g-1", NOTE_BODY, trigger=TRIGGER)
        assert created == NOTE_CREATED

    def test_list_notes_returns_the_notes_list(self, transport, log_dir) -> None:
        in_scope = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": in_scope})
        transport.route("GET", "/contacts/g-1/notes", {"notes": [NOTE_CREATED]})
        notes = client(transport, log_dir).list_notes("g-1")
        assert notes == [NOTE_CREATED]

    def test_get_note_returns_the_single_note(self, transport, log_dir) -> None:
        in_scope = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": in_scope})
        transport.route("GET", "/contacts/g-1/notes/note-1", {"note": NOTE_CREATED})
        note = client(transport, log_dir).get_note("g-1", "note-1")
        assert note == NOTE_CREATED

    def test_list_notes_and_get_note_write_no_audit_line(self, transport, log_dir) -> None:
        in_scope = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": in_scope})
        transport.route("GET", "/contacts/g-1/notes", {"notes": [NOTE_CREATED]})
        transport.route("GET", "/contacts/g-1/notes/note-1", {"note": NOTE_CREATED})
        c = client(transport, log_dir)
        c.list_notes("g-1")
        c.get_note("g-1", "note-1")
        assert entries(log_dir) == []

    def test_empty_body_is_refused_before_the_gate(self, transport, log_dir) -> None:
        in_scope = {**CONTACT_BEFORE, "locationId": LOCATION_ID}
        transport.route("GET", "/contacts/g-1", {"contact": in_scope})
        with pytest.raises(ValueError, match="non-empty body"):
            client(transport, log_dir).create_note("g-1", "  ", trigger=TRIGGER)
        assert entries(log_dir) == []
        assert [c for c in transport.writes if "/notes" in c.url] == []

    def test_update_note_returns_the_updated_note(self, transport, log_dir) -> None:
        route_update_note(transport)
        updated = client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER
        )
        assert updated == NOTE_AFTER


class TestNoDeleteNoteCapability:
    def test_the_client_exposes_no_note_deletion(self, transport, log_dir) -> None:
        ghl = client(transport, log_dir)
        assert hasattr(ghl, "update_note")
        assert not hasattr(ghl, "delete_note")


# ── OPS-18: green call-note color + safe field preservation ──────────────────


class TestCreateNoteColorAndPinned:
    def test_default_create_note_call_has_no_color_and_is_unpinned(
        self, transport, log_dir
    ) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note("g-1", NOTE_BODY, trigger=TRIGGER)
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body == {"body": NOTE_BODY, "pinned": False}

    def test_create_note_writes_the_given_color_and_stays_unpinned_by_default(
        self, transport, log_dir
    ) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note(
            "g-1", NOTE_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR
        )
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body == {
            "body": NOTE_BODY, "pinned": False, "color": CALL_NOTE_COLOR,
        }

    def test_create_note_pinned_true_is_passed_through(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note(
            "g-1", NOTE_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR, pinned=True
        )
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body == {
            "body": NOTE_BODY, "pinned": True, "color": CALL_NOTE_COLOR,
        }

    def test_create_note_title_is_omitted_by_default(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note("g-1", NOTE_BODY, trigger=TRIGGER)
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert "title" not in post.json_body

    def test_create_note_writes_the_given_title(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note(
            "g-1", NOTE_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR,
            title="Incoming call · Answered · 21 min 46 sec",
        )
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body == {
            "body": NOTE_BODY, "pinned": False, "color": CALL_NOTE_COLOR,
            "title": "Incoming call · Answered · 21 min 46 sec",
        }


class TestUpdateNotePreservesUnrelatedFields:
    """update_note() must never clear userId/title/pinned/color the caller
    didn't ask to change -- GHL's note PUT has replace semantics, so an
    omitted field is read from the pre-write GET and carried forward."""

    def test_body_and_color_only_preserves_userid_title_and_pinned(
        self, transport, log_dir
    ) -> None:
        route_update_note_full(transport)
        client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR
        )
        put = [c for c in transport.writes if c.method == "PUT"][0]
        assert put.json_body == {
            "body": NOTE_UPDATED_BODY,
            "pinned": True,
            "userId": "user-123",
            "title": "Call log",
            "color": CALL_NOTE_COLOR,
        }

    def test_this_is_the_exact_authorized_live_update_shape(
        self, transport, log_dir
    ) -> None:
        """The Cory-note correction Clay authorized changes body and color
        only -- everything else on the call must come from before-state."""
        route_update_note_full(transport)
        result = client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR
        )
        assert result == NOTE_AFTER_GREEN

    def test_explicit_overrides_win_over_before_state(self, transport, log_dir) -> None:
        route_update_note_full(transport)
        client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER,
            color=CALL_NOTE_COLOR, pinned=False, userId="user-999", title="Renamed",
        )
        put = [c for c in transport.writes if c.method == "PUT"][0]
        assert put.json_body == {
            "body": NOTE_UPDATED_BODY,
            "pinned": False,
            "userId": "user-999",
            "title": "Renamed",
            "color": CALL_NOTE_COLOR,
        }

    def test_before_state_with_no_optional_fields_defaults_pinned_false_and_omits_the_rest(
        self, transport, log_dir
    ) -> None:
        route_update_note(transport)  # NOTE_BEFORE has no userId/title/pinned/color
        client(transport, log_dir).update_note(
            "g-1", "note-1", NOTE_UPDATED_BODY, trigger=TRIGGER, color=CALL_NOTE_COLOR
        )
        put = [c for c in transport.writes if c.method == "PUT"][0]
        assert put.json_body == {
            "body": NOTE_UPDATED_BODY,
            "pinned": False,
            "color": CALL_NOTE_COLOR,
        }
