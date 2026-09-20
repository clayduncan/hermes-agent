"""Isolated tests for the team_duncan_contacts plugin.

All tests use fakes and temporary directories.  No live GHL calls, no real
credentials, no live profile data is read or written.

Test categories (matching spec section 'Required tests'):
 1. Agent tool schemas reject phone, email, and Apple-handle inputs.
 2. Prepare returns only masked handles and exact GHL/location IDs.
 3. Unique contact prepares successfully; zero and multiple matches write nothing.
 4. Confirmation timestamp comes from the confirmation transaction, not preparation.
 5. Confirmation replay is idempotent and cannot move the cutoff.
 6. Original activation timestamp cannot be edited, reset, or backdated.
 7. One instant before cutoff denies; exact cutoff allows; after cutoff allows.
 8. Pause/resume/retire lifecycle preserves history and enforces state rules.
 9. Missing and ambiguous HMAC mappings return review_required and write nothing.
10. Canary phone handle resolves correctly; raw handle absent from all surfaces.
11. State and key files enforce owner-only permissions.
12. Restart persistence and corruption fail-closed behavior.
13. No GHL write method reachable from activation or resolver paths.
14. Marker: existing plugin/GHL/audit/redaction tests unaffected (no action needed).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.registry import (
    ContactRegistry,
    CorruptStateError,
    InvalidInputError,
    MissingHmacKeyError,
    PrepareResult,
    ConfirmResult,
    ResolveResult,
    RetiredContactError,
    validate_agent_name_or_id,
)
from plugins.team_duncan_contacts.sanitizer import contains_phone_like, sanitize_output
from plugins.team_duncan_contacts.tools import (
    CONFIRM_ACTIVATION_SCHEMA,
    PREPARE_ACTIVATION_SCHEMA,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCATION_ID = "loc-team-duncan-123"
ALT_LOCATION = "loc-other-456"

# A synthetic canary phone number: must never appear outside registry boundary
CANARY_PHONE = "+15551239999"

CONTACT_ALICE = {
    "id": "ghl-alice-001",
    "locationId": LOCATION_ID,
    "firstName": "Alice",
    "lastName": "Anderson",
    "phone": CANARY_PHONE,
    "email": "alice@example.com",
}

CONTACT_BOB = {
    "id": "ghl-bob-002",
    "locationId": LOCATION_ID,
    "firstName": "Bob",
    "lastName": "Baker",
    "phone": "+15559998888",
}

CONTACT_CHARLIE_WRONG_LOCATION = {
    "id": "ghl-charlie-003",
    "locationId": ALT_LOCATION,
    "firstName": "Charlie",
    "lastName": "Carson",
    "phone": "+15557776666",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def make_clock(ts: datetime):
    """Return a clock function that always returns *ts*."""
    box = [ts]

    def _clock() -> datetime:
        return box[0]

    def _advance(seconds: float) -> None:
        box[0] = box[0] + timedelta(seconds=seconds)

    _clock.advance = _advance
    return _clock


def make_registry(
    tmp_path: Path,
    location_id: str = LOCATION_ID,
    clock=None,
) -> ContactRegistry:
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    return ContactRegistry(
        hermes_home,
        team_duncan_location_id=location_id,
        clock=clock,
    )


def make_reader(contacts=None) -> FakeGhlReader:
    return FakeGhlReader(contacts or [CONTACT_ALICE, CONTACT_BOB])


def activate_contact(
    registry: ContactRegistry,
    contact: dict,
    reader: FakeGhlReader | None = None,
    name_query: str | None = None,
) -> ConfirmResult:
    """Helper: run the full prepare+confirm flow for a contact."""
    if reader is None:
        reader = FakeGhlReader([contact])
    query = name_query or f"{contact['firstName']} {contact['lastName']}"
    prep = registry.prepare_activation(query, reader)
    assert prep.status == "ready_for_confirmation", f"Unexpected: {prep.to_dict()}"
    return registry.confirm_activation(prep.token)


# ---------------------------------------------------------------------------
# 1. Schema rejects phone/email/Apple handles
# ---------------------------------------------------------------------------


class TestSchemaInputRejection:
    """Spec §Required tests item 1."""

    @pytest.mark.parametrize(
        "bad_input",
        [
            "+1 (555) 123-4567",
            "555-867-5309",
            "15558675309",
            "+15558675309",
            "(555) 867-5309",
            "5558675309",
            "555 867 5309",
        ],
    )
    def test_phone_numbers_rejected_raises(self, bad_input: str) -> None:
        with pytest.raises(InvalidInputError, match="Phone"):
            validate_agent_name_or_id(bad_input)

    @pytest.mark.parametrize(
        "bad_input",
        [
            "alice@example.com",
            "contact@team-duncan.co",
            "someone+tag@mail.org",
        ],
    )
    def test_email_rejected(self, bad_input: str) -> None:
        with pytest.raises(InvalidInputError, match="Email"):
            validate_agent_name_or_id(bad_input)

    @pytest.mark.parametrize(
        "bad_input",
        [
            "tel:+15551234567",
            "imessage:alice@example.com",
            "icloud:someone@icloud.com",
        ],
    )
    def test_apple_handles_rejected(self, bad_input: str) -> None:
        with pytest.raises(InvalidInputError, match="Apple"):
            validate_agent_name_or_id(bad_input)

    @pytest.mark.parametrize(
        "good_input",
        [
            "Alice Anderson",
            "Bob Baker",
            "ghl-alice-001",
            "abc123XYZ456",
            "Nathan",
        ],
    )
    def test_valid_names_and_ids_accepted(self, good_input: str) -> None:
        result = validate_agent_name_or_id(good_input)
        assert result == good_input.strip()

    def test_prepare_rejects_phone_via_registry(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader()
        result = reg.prepare_activation("+15551234567", reader)
        assert result.status == "invalid_input"
        assert result.token is None

    def test_prepare_rejects_email_via_registry(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader()
        result = reg.prepare_activation("alice@example.com", reader)
        assert result.status == "invalid_input"
        assert result.token is None

    def test_tool_schema_has_no_phone_field(self) -> None:
        params = PREPARE_ACTIVATION_SCHEMA["function"]["parameters"]
        props = params.get("properties", {})
        assert "phone" not in props
        assert "email" not in props
        assert "handle" not in props

    def test_confirm_schema_has_only_token_field(self) -> None:
        params = CONFIRM_ACTIVATION_SCHEMA["function"]["parameters"]
        props = params.get("properties", {})
        assert set(props.keys()) == {"token"}


# ---------------------------------------------------------------------------
# 2. Prepare returns only masked handles, exact GHL/location IDs
# ---------------------------------------------------------------------------


class TestPrepareReturnsMaskedOnly:
    """Spec §Required tests item 2."""

    def test_prepare_returns_masked_phone(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Alice Anderson", reader)

        assert result.status == "ready_for_confirmation"
        d = result.to_dict()

        # Exact IDs must be present
        assert d["contact_id"] == CONTACT_ALICE["id"]
        assert d["location_id"] == LOCATION_ID

        # Raw phone must NOT appear anywhere in output
        assert CANARY_PHONE not in json.dumps(d)

        # Masked label must be present
        assert "phone" in d["masked_handles"]
        mask = d["masked_handles"]["phone"]
        assert "***" in mask
        assert re.search(r"\d{4}$", mask), f"Last 4 digits missing from mask: {mask}"

    def test_prepare_dict_contains_no_raw_phone(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Alice Anderson", reader)
        serialized = json.dumps(result.to_dict())
        assert CANARY_PHONE not in serialized

    def test_prepare_contains_masked_email(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Alice Anderson", reader)
        d = result.to_dict()
        assert "email" in d.get("masked_handles", {})
        mask = d["masked_handles"]["email"]
        assert "@" in mask
        assert "alice@example.com" not in mask


# ---------------------------------------------------------------------------
# 3. Unique match prepares; zero / multiple write nothing
# ---------------------------------------------------------------------------


class TestPrepareMatchSemantics:
    """Spec §Required tests item 3."""

    def test_unique_match_succeeds(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Alice Anderson", reader)
        assert result.status == "ready_for_confirmation"
        assert result.token is not None

    def test_zero_matches_returns_contact_creation_required_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Nobody NoName", reader)
        assert result.status == "contact_creation_required"
        assert result.reason == "no_ghl_contact"
        assert result.token is None
        # State file should not exist
        state_path = reg._state_path
        assert not state_path.exists() or _load_state(state_path)["contacts"] == {}

    def test_multiple_matches_returns_review_required_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        # Two contacts with similar names
        c1 = {**CONTACT_ALICE, "id": "dup-1"}
        c2 = {**CONTACT_ALICE, "id": "dup-2", "firstName": "Alicia"}
        reg = make_registry(tmp_path)
        reader = make_reader([c1, c2])
        # Search "alice" matches both
        result = reg.prepare_activation("Alice", reader)
        if result.status == "review_required" and result.reason == "multiple_matches":
            assert result.token is None
        # If only one matched, that's fine too: test the multiple case explicitly
        # by crafting a reader that always returns two
        class _AlwaysTwoReader(FakeGhlReader):
            def search_contacts_by_name(self, query, location_id):
                return [c1, c2]

        reg2 = make_registry(tmp_path / "reg2")
        result2 = reg2.prepare_activation("Alice", _AlwaysTwoReader([c1, c2]))
        assert result2.status == "review_required"
        assert result2.reason == "multiple_matches"
        assert result2.token is None

    def test_wrong_location_by_id_returns_review_required(self, tmp_path: Path) -> None:
        # Contact found by exact GHL ID but belongs to a different location
        reg = make_registry(tmp_path, location_id=LOCATION_ID)
        reader = make_reader([CONTACT_CHARLIE_WRONG_LOCATION])
        result = reg.prepare_activation(CONTACT_CHARLIE_WRONG_LOCATION["id"], reader)
        assert result.status == "review_required"
        assert result.reason == "wrong_location"
        assert result.token is None

    def test_wrong_location_name_query_returns_contact_creation_required(
        self, tmp_path: Path
    ) -> None:
        # Name search is location-scoped; a wrong-location contact is invisible
        # to name search and is indistinguishable from "no GHL contact at all"
        reg = make_registry(tmp_path, location_id=LOCATION_ID)
        reader = make_reader([CONTACT_CHARLIE_WRONG_LOCATION])
        result = reg.prepare_activation("Charlie Carson", reader)
        assert result.status == "contact_creation_required"
        assert result.token is None

    def test_lookup_by_exact_id(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation(CONTACT_ALICE["id"], reader)
        assert result.status == "ready_for_confirmation"
        assert result.contact_id == CONTACT_ALICE["id"]


# ---------------------------------------------------------------------------
# 3b. contact_creation_required: criterion 12 (OPS-16 prerequisite)
# ---------------------------------------------------------------------------


class TestContactCreationRequired:
    """Criterion 12: missing GHL contact returns contact_creation_required with OPS-16.

    When a name or ID is provided but no matching GHL contact exists at all,
    prepare_activation must return status='contact_creation_required' and the
    message must reference OPS-16 as the prerequisite workflow.  No state is
    written in this case.
    """

    def test_missing_ghl_contact_returns_contact_creation_required(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])  # Alice is the only contact
        result = reg.prepare_activation("Ghost Person", reader)
        assert result.status == "contact_creation_required", (
            f"Expected contact_creation_required, got {result.status!r}: {result.to_dict()}"
        )
        assert result.token is None

    def test_contact_creation_required_references_ops16(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Nonexistent Person", reader)
        assert result.status == "contact_creation_required"
        assert "OPS-16" in result.message, (
            f"OPS-16 not referenced in message: {result.message!r}"
        )

    def test_contact_creation_required_has_no_ghl_contact_reason(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Nobody Here", reader)
        assert result.status == "contact_creation_required"
        assert result.reason == "no_ghl_contact"

    def test_contact_creation_required_writes_no_state(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("Missing Contact", reader)
        assert result.status == "contact_creation_required"
        assert result.token is None
        state_path = reg._state_path
        assert not state_path.exists() or _load_state(state_path)["contacts"] == {}

    def test_unknown_ghl_id_returns_contact_creation_required(
        self, tmp_path: Path
    ) -> None:
        # An ID that doesn't match any GHL contact (not just wrong-location)
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        result = reg.prepare_activation("ghl-does-not-exist-999", reader)
        assert result.status == "contact_creation_required"
        assert result.token is None


# ---------------------------------------------------------------------------
# 4. Confirmation timestamp from confirmation transaction, not preparation
# ---------------------------------------------------------------------------


class TestConfirmationTimestamp:
    """Spec §Required tests item 4."""

    def test_activated_at_is_confirmation_time_not_prepare_time(
        self, tmp_path: Path
    ) -> None:
        t_prepare = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_confirm = datetime(2024, 1, 1, 10, 3, 0, tzinfo=timezone.utc)

        clock = make_clock(t_prepare)
        reg = make_registry(tmp_path, clock=clock)
        reader = make_reader([CONTACT_ALICE])

        prep = reg.prepare_activation("Alice Anderson", reader)
        assert prep.status == "ready_for_confirmation"

        # Advance clock to confirmation time
        clock.advance(180)

        confirm = reg.confirm_activation(prep.token)
        assert confirm.status == "activated"

        activated_at = datetime.fromisoformat(confirm.activated_at)
        # Must be at or after t_confirm (clock was at t_prepare + 180s)
        assert activated_at >= t_confirm, (
            f"activated_at {activated_at} should be >= confirmation time {t_confirm}"
        )
        # Must NOT equal t_prepare
        assert activated_at != t_prepare


# ---------------------------------------------------------------------------
# 5. Confirmation replay is idempotent and cannot move the cutoff
# ---------------------------------------------------------------------------


class TestConfirmationIdempotency:
    """Spec §Required tests item 5."""

    def test_replay_returns_already_activated(self, tmp_path: Path) -> None:
        clock = make_clock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)
        reader = make_reader([CONTACT_ALICE])

        prep = reg.prepare_activation("Alice Anderson", reader)
        confirm1 = reg.confirm_activation(prep.token)
        assert confirm1.status == "activated"
        original_ts = confirm1.activated_at

        # Advance time and re-prepare with a new token for same contact
        clock.advance(60)
        prep2 = reg.prepare_activation("Alice Anderson", reader)
        assert prep2.status == "ready_for_confirmation"

        # Confirm again: must be idempotent
        clock.advance(60)
        confirm2 = reg.confirm_activation(prep2.token)
        assert confirm2.status == "already_activated"
        assert confirm2.activated_at == original_ts, (
            f"Cutoff moved! original={original_ts}, new={confirm2.activated_at}"
        )

    def test_original_token_replay_also_idempotent(self, tmp_path: Path) -> None:
        clock = make_clock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)

        # Artificially re-insert the token to simulate replay
        reader = make_reader([CONTACT_ALICE])
        prep = reg.prepare_activation("Alice Anderson", reader)
        original_token = prep.token

        confirm1 = reg.confirm_activation(original_token)
        original_ts = confirm1.activated_at

        # The token is consumed, so replaying it returns token_not_found_or_expired
        # (which is correct: token was consumed on first confirm)
        confirm2 = reg.confirm_activation(original_token)
        assert confirm2.status in ("already_activated", "error")
        # Either way, the cutoff must not move
        if confirm2.status == "already_activated":
            assert confirm2.activated_at == original_ts


# ---------------------------------------------------------------------------
# 6. Original activation timestamp cannot be edited/reset/backdated
# ---------------------------------------------------------------------------


class TestActivationTimestampImmutability:
    """Spec §Required tests item 6."""

    def test_activated_at_not_in_transition_history(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        original_ts = contact["activated_at"]

        # Pause and resume
        reg.pause_contact(CONTACT_ALICE["id"], "clay", "test pause")
        reg.resume_contact(CONTACT_ALICE["id"], "clay", "test resume")

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        # activated_at must still be the original
        assert contact["activated_at"] == original_ts

    def test_transition_history_appends_not_replaces(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        reg.pause_contact(CONTACT_ALICE["id"], "clay", "testing")
        reg.resume_contact(CONTACT_ALICE["id"], "clay", "testing")
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "testing")

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]

        transitions = [h["transition"] for h in contact["history"]]
        assert transitions == ["activated", "paused", "active", "retired"]

    def test_activated_at_preserved_through_all_transitions(
        self, tmp_path: Path
    ) -> None:
        clock = make_clock(datetime(2024, 3, 15, 9, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)
        reader = make_reader([CONTACT_ALICE])

        prep = reg.prepare_activation("Alice Anderson", reader)
        clock.advance(10)
        confirm = reg.confirm_activation(prep.token)
        original_ts = confirm.activated_at

        clock.advance(3600)
        reg.pause_contact(CONTACT_ALICE["id"], "clay", "vacation")
        clock.advance(86400)
        reg.resume_contact(CONTACT_ALICE["id"], "clay", "back")

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        assert contact["activated_at"] == original_ts


# ---------------------------------------------------------------------------
# 7. Cutoff boundary decisions
# ---------------------------------------------------------------------------


class TestCutoffBoundary:
    """Spec §Required tests item 7."""

    def _setup(self, tmp_path: Path):
        cutoff_ts = datetime(2024, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
        clock = make_clock(cutoff_ts - timedelta(minutes=5))
        reg = make_registry(tmp_path, clock=clock)
        reader = make_reader([CONTACT_ALICE])

        prep = reg.prepare_activation("Alice Anderson", reader)
        clock.advance(10)
        confirm = reg.confirm_activation(prep.token)
        actual_cutoff = datetime.fromisoformat(confirm.activated_at)
        return reg, actual_cutoff

    def test_one_second_before_cutoff_denies(self, tmp_path: Path) -> None:
        reg, cutoff = self._setup(tmp_path)
        event_ts = cutoff - timedelta(seconds=1)
        result = reg.resolve_event(CANARY_PHONE, event_ts)
        assert result.decision == "deny_pre_activation"
        assert not result.authorized

    def test_exact_cutoff_allows(self, tmp_path: Path) -> None:
        reg, cutoff = self._setup(tmp_path)
        result = reg.resolve_event(CANARY_PHONE, cutoff)
        assert result.decision == "allow"
        assert result.authorized
        assert result.cutoff_decision == "at_cutoff"

    def test_one_second_after_cutoff_allows(self, tmp_path: Path) -> None:
        reg, cutoff = self._setup(tmp_path)
        event_ts = cutoff + timedelta(seconds=1)
        result = reg.resolve_event(CANARY_PHONE, event_ts)
        assert result.decision == "allow"
        assert result.authorized
        assert result.cutoff_decision == "after_cutoff"


# ---------------------------------------------------------------------------
# 8. Lifecycle: pause / resume / retire
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Spec §Required tests item 8."""

    def _activate_alice(self, tmp_path: Path, clock=None) -> ContactRegistry:
        reg = make_registry(tmp_path, clock=clock)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)
        return reg

    def test_pause_denies_new_events(self, tmp_path: Path) -> None:
        reg = self._activate_alice(tmp_path)
        reg.pause_contact(CONTACT_ALICE["id"], "clay", "testing")
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        result = reg.resolve_event(CANARY_PHONE, future)
        assert result.decision == "deny_paused"

    def test_pause_preserves_history(self, tmp_path: Path) -> None:
        reg = self._activate_alice(tmp_path)
        reg.pause_contact(CONTACT_ALICE["id"], "clay", "vacation")

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        transitions = [h["transition"] for h in contact["history"]]
        assert "activated" in transitions
        assert "paused" in transitions

    def test_reactivation_preserves_original_cutoff(self, tmp_path: Path) -> None:
        clock = make_clock(datetime(2024, 4, 1, 10, 0, 0, tzinfo=timezone.utc))
        reg = self._activate_alice(tmp_path, clock=clock)

        state = _load_state(reg._state_path)
        original_cutoff = state["contacts"][CONTACT_ALICE["id"]]["activated_at"]

        reg.pause_contact(CONTACT_ALICE["id"], "clay", "test")
        clock.advance(3600)
        reg.resume_contact(CONTACT_ALICE["id"], "clay", "back")

        state = _load_state(reg._state_path)
        assert state["contacts"][CONTACT_ALICE["id"]]["activated_at"] == original_cutoff

    def test_retirement_denies_events(self, tmp_path: Path) -> None:
        reg = self._activate_alice(tmp_path)
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "done")
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        result = reg.resolve_event(CANARY_PHONE, future)
        assert result.decision == "deny_retired"

    def test_retirement_is_terminal(self, tmp_path: Path) -> None:
        reg = self._activate_alice(tmp_path)
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "terminal")
        with pytest.raises(RetiredContactError):
            reg.resume_contact(CONTACT_ALICE["id"], "clay", "attempt to reactivate")

    def test_retire_preserves_history(self, tmp_path: Path) -> None:
        reg = self._activate_alice(tmp_path)
        reg.pause_contact(CONTACT_ALICE["id"], "clay", "pause before retire")
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "final")

        state = _load_state(reg._state_path)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        transitions = [h["transition"] for h in contact["history"]]
        assert transitions == ["activated", "paused", "retired"]


# ---------------------------------------------------------------------------
# 9. Missing and ambiguous HMAC mappings
# ---------------------------------------------------------------------------


class TestHmacMappingEdgeCases:
    """Spec §Required tests item 9."""

    def test_unknown_handle_returns_review_required(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        unknown_phone = "+19999999999"
        result = reg.resolve_event(
            unknown_phone, datetime.now(tz=timezone.utc) + timedelta(hours=1)
        )
        assert result.decision == "review_required"
        assert not result.authorized

    def test_ambiguous_hmac_returns_review_required_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        # Two contacts with the SAME phone (should not happen, but must handle safely)
        c_dup1 = {**CONTACT_ALICE, "id": "dup-1"}
        c_dup2 = {**CONTACT_BOB, "id": "dup-2", "phone": CANARY_PHONE}

        reg = make_registry(tmp_path)
        activate_contact(reg, c_dup1, FakeGhlReader([c_dup1]))
        activate_contact(reg, c_dup2, FakeGhlReader([c_dup2]))

        result = reg.resolve_event(
            CANARY_PHONE, datetime.now(tz=timezone.utc) + timedelta(hours=1)
        )
        assert result.decision == "review_required"

    def test_no_activated_contacts_returns_review_required(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        result = reg.resolve_event(CANARY_PHONE, datetime.now(tz=timezone.utc))
        assert result.decision == "review_required"

    def test_review_required_writes_no_state(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        state_before = _load_state(reg._state_path)
        reg.resolve_event("+19999999999", datetime.now(tz=timezone.utc))
        state_after = _load_state(reg._state_path)

        assert state_before["contacts"] == state_after["contacts"]


# ---------------------------------------------------------------------------
# 10. Canary handle: raw value absent from all surfaces
# ---------------------------------------------------------------------------


class TestCanaryPhoneAbsence:
    """Spec §Required tests item 10."""

    def _run_full_flow(self, tmp_path: Path):
        clock = make_clock(datetime(2024, 7, 1, 9, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)
        reader = FakeGhlReader([CONTACT_ALICE])

        # Step 1: prepare
        prep_result = reg.prepare_activation("Alice Anderson", reader)
        assert prep_result.status == "ready_for_confirmation"

        # Step 2: confirm
        clock.advance(10)
        confirm_result = reg.confirm_activation(prep_result.token)
        assert confirm_result.status == "activated"

        # Step 3: resolve event using canary phone
        event_ts = datetime.fromisoformat(confirm_result.activated_at) + timedelta(
            seconds=1
        )
        resolve_result = reg.resolve_event(CANARY_PHONE, event_ts)
        assert resolve_result.authorized

        return prep_result, confirm_result, resolve_result

    def test_canary_absent_from_prepare_output(self, tmp_path: Path) -> None:
        prep, _, _ = self._run_full_flow(tmp_path)
        serialized = json.dumps(prep.to_dict())
        assert CANARY_PHONE not in serialized, (
            f"Raw canary phone found in prepare output: {serialized}"
        )

    def test_canary_absent_from_confirm_output(self, tmp_path: Path) -> None:
        _, confirm, _ = self._run_full_flow(tmp_path)
        serialized = json.dumps(confirm.to_dict())
        assert CANARY_PHONE not in serialized, (
            f"Raw canary phone found in confirm output: {serialized}"
        )

    def test_canary_absent_from_resolve_output(self, tmp_path: Path) -> None:
        _, _, resolve = self._run_full_flow(tmp_path)
        # ResolveResult contains only masked_metadata, not raw handles
        import dataclasses
        for attr in ("decision", "registry_contact_id", "ghl_contact_id",
                     "location_id", "lifecycle_state", "cutoff_decision", "message"):
            val = str(getattr(resolve, attr) or "")
            assert CANARY_PHONE not in val
        # masked_metadata
        meta_str = json.dumps(resolve.masked_metadata)
        assert CANARY_PHONE not in meta_str

    def test_canary_absent_from_state_file(self, tmp_path: Path) -> None:
        self._run_full_flow(tmp_path)
        state_path = (
            tmp_path / "hermes_home" / "plugin-data" / "team_duncan_contacts" / "registry.json"
        )
        assert state_path.exists()
        state_text = state_path.read_text(encoding="utf-8")
        assert CANARY_PHONE not in state_text, (
            f"Raw canary phone found in state file: {state_text}"
        )

    def test_canary_absent_from_logs(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.DEBUG):
            self._run_full_flow(tmp_path)
        log_text = caplog.text
        assert CANARY_PHONE not in log_text, (
            f"Raw canary phone found in log output: {log_text}"
        )

    def test_canary_absent_from_stdout_stderr(self, tmp_path: Path) -> None:
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            self._run_full_flow(tmp_path)
        assert CANARY_PHONE not in stdout_buf.getvalue()
        assert CANARY_PHONE not in stderr_buf.getvalue()

    def test_resolve_resolves_to_correct_contact_id(self, tmp_path: Path) -> None:
        _, _, resolve = self._run_full_flow(tmp_path)
        assert resolve.ghl_contact_id == CONTACT_ALICE["id"]
        assert resolve.authorized


# ---------------------------------------------------------------------------
# 11. File permission enforcement
# ---------------------------------------------------------------------------


class TestFilePermissions:
    """Spec §Required tests item 11."""

    @pytest.mark.skipif(os.name == "nt", reason="chmod semantics differ on Windows")
    def test_hmac_key_file_is_owner_only(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        kp = reg._key_path
        assert kp.exists()
        mode = stat.S_IMODE(kp.stat().st_mode)
        assert mode == 0o600, f"Key file mode is {mode:04o}, expected 0o600"

    @pytest.mark.skipif(os.name == "nt", reason="chmod semantics differ on Windows")
    def test_state_file_is_owner_only(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        sp = reg._state_path
        assert sp.exists()
        mode = stat.S_IMODE(sp.stat().st_mode)
        assert mode == 0o600, f"State file mode is {mode:04o}, expected 0o600"

    @pytest.mark.skipif(os.name == "nt", reason="chmod semantics differ on Windows")
    def test_startup_validate_rejects_bad_key_permissions(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        # Corrupt permissions
        os.chmod(reg._key_path, 0o644)

        reg2 = make_registry(tmp_path)
        with pytest.raises(CorruptStateError, match="insecure"):
            reg2.startup_validate()

        # Restore so test cleanup works
        os.chmod(reg._key_path, 0o600)


# ---------------------------------------------------------------------------
# 12. Restart persistence and corruption fail-closed
# ---------------------------------------------------------------------------


class TestRestartPersistence:
    """Spec §Required tests item 12."""

    def test_state_persists_across_registry_restart(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()

        reg1 = ContactRegistry(
            hermes_home,
            team_duncan_location_id=LOCATION_ID,
        )
        reader = make_reader([CONTACT_ALICE])
        confirm = activate_contact(reg1, CONTACT_ALICE, reader)
        original_ts = confirm.activated_at

        # Simulate restart: create a new registry instance pointing at the same home
        reg2 = ContactRegistry(
            hermes_home,
            team_duncan_location_id=LOCATION_ID,
        )
        reg2.startup_validate()

        # Contact should still be present
        state = _load_state(reg2._state_path)
        assert CONTACT_ALICE["id"] in state["contacts"]
        assert state["contacts"][CONTACT_ALICE["id"]]["activated_at"] == original_ts

    def test_resolve_works_after_restart(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()

        reg1 = ContactRegistry(
            hermes_home,
            team_duncan_location_id=LOCATION_ID,
        )
        reader = make_reader([CONTACT_ALICE])
        confirm = activate_contact(reg1, CONTACT_ALICE, reader)
        cutoff = datetime.fromisoformat(confirm.activated_at)

        reg2 = ContactRegistry(
            hermes_home,
            team_duncan_location_id=LOCATION_ID,
        )
        event_ts = cutoff + timedelta(seconds=60)
        result = reg2.resolve_event(CANARY_PHONE, event_ts)
        assert result.authorized

    def test_corrupt_state_file_raises_on_load(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        # Write garbage to state file
        reg._state_path.parent.mkdir(parents=True, exist_ok=True)
        reg._state_path.write_bytes(b"not valid json {{{{")

        reg2 = make_registry(tmp_path)
        with pytest.raises(CorruptStateError):
            reg2._load_state_raw()

    def test_missing_key_with_existing_contacts_fails_closed(
        self, tmp_path: Path
    ) -> None:
        reg = make_registry(tmp_path)
        reader = make_reader([CONTACT_ALICE])
        activate_contact(reg, CONTACT_ALICE, reader)

        # Delete the key file to simulate loss
        reg._key_path.unlink()

        reg2 = make_registry(tmp_path)
        # Startup validate should raise
        with pytest.raises(MissingHmacKeyError):
            reg2.startup_validate()

    def test_export_restore_round_trip(self, tmp_path: Path) -> None:
        hermes_home1 = tmp_path / "home1"
        hermes_home1.mkdir()
        hermes_home2 = tmp_path / "home2"
        hermes_home2.mkdir()

        reg1 = ContactRegistry(
            hermes_home1, team_duncan_location_id=LOCATION_ID
        )
        reader = make_reader([CONTACT_ALICE])
        confirm = activate_contact(reg1, CONTACT_ALICE, reader)
        original_ts = confirm.activated_at

        # Export from reg1
        export_data = reg1.export_state()
        assert CANARY_PHONE not in json.dumps(export_data)

        # Restore into reg2 (with the same HMAC key)
        # Copy the key file manually (simulating backup restore)
        dest_data_dir = hermes_home2 / "plugin-data" / "team_duncan_contacts"
        dest_data_dir.mkdir(parents=True)
        key_bytes = reg1._key_path.read_bytes()
        key_dest = dest_data_dir / "hmac_key"
        key_dest.write_bytes(key_bytes)
        os.chmod(key_dest, 0o600)

        reg2 = ContactRegistry(
            hermes_home2, team_duncan_location_id=LOCATION_ID
        )
        reg2.restore_state(export_data)

        state = _load_state(reg2._state_path)
        assert CONTACT_ALICE["id"] in state["contacts"]
        assert state["contacts"][CONTACT_ALICE["id"]]["activated_at"] == original_ts

        # Resolve should work in reg2 too (same key)
        cutoff = datetime.fromisoformat(original_ts)
        result = reg2.resolve_event(CANARY_PHONE, cutoff + timedelta(seconds=1))
        assert result.authorized

    def test_wrong_schema_version_raises(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        sp = reg._state_path
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(
            json.dumps({"schema_version": 99, "contacts": {}, "pending_tokens": {}}),
            encoding="utf-8",
        )
        with pytest.raises(CorruptStateError, match="schema version"):
            reg.startup_validate()


# ---------------------------------------------------------------------------
# 13. No GHL write method reachable from activation / resolver paths
# ---------------------------------------------------------------------------


class TestNoGhlWriteAccess:
    """Spec §Required tests item 13."""

    def test_ghl_write_client_not_imported_by_registry(self) -> None:
        """registry.py must not import the GHL write client."""
        import plugins.team_duncan_contacts.registry as reg_mod
        import sys

        # ghl_client exposes GoHighLevelWriteClient
        ghl_client_name = "tools.ghl_client"
        # Check that the registry module did not import ghl_client
        registry_globals = vars(reg_mod)
        for name, obj in registry_globals.items():
            if hasattr(obj, "__module__") and obj.__module__ == ghl_client_name:
                pytest.fail(
                    f"GHL write client object {name!r} found in registry module globals"
                )

        assert ghl_client_name not in getattr(reg_mod, "__dict__", {}).get(
            "__imports__", {}
        )

    def test_write_audit_recorder_not_imported_by_registry(self) -> None:
        """registry.py must not import WriteAuditRecorder."""
        import plugins.team_duncan_contacts.registry as reg_mod

        for name, obj in vars(reg_mod).items():
            if "WriteAudit" in type(obj).__name__ or "WriteAudit" in str(
                getattr(obj, "__name__", "")
            ):
                pytest.fail(
                    f"WriteAuditRecorder-related object {name!r} found in registry globals"
                )

    def test_activation_flow_makes_no_ghl_writes(self, tmp_path: Path) -> None:
        """Activation flow must not call any GHL write method."""
        from unittest.mock import patch, MagicMock

        # Patch the GHL write client to detect any instantiation or call
        mock_write_client = MagicMock()
        with patch("tools.ghl_client.GoHighLevelWriteClient", mock_write_client):
            reg = make_registry(tmp_path)
            reader = make_reader([CONTACT_ALICE])
            activate_contact(reg, CONTACT_ALICE, reader)

        mock_write_client.assert_not_called()

    def test_resolver_makes_no_ghl_writes(self, tmp_path: Path) -> None:
        """Resolver path must not call any GHL write method."""
        from unittest.mock import patch, MagicMock

        mock_write_client = MagicMock()
        with patch("tools.ghl_client.GoHighLevelWriteClient", mock_write_client):
            reg = make_registry(tmp_path)
            reader = make_reader([CONTACT_ALICE])
            activate_contact(reg, CONTACT_ALICE, reader)
            reg.resolve_event(CANARY_PHONE, datetime.now(tz=timezone.utc))

        mock_write_client.assert_not_called()


# ---------------------------------------------------------------------------
# Sanitizer unit tests
# ---------------------------------------------------------------------------


class TestSanitizer:
    def test_phone_in_string_redacted(self) -> None:
        result = sanitize_output("Call me at +15551234567 please")
        assert "+15551234567" not in result
        assert "[PHONE REDACTED]" in result

    def test_nested_dict_redacted(self) -> None:
        data = {"note": "Phone is 555-867-5309", "name": "Alice"}
        result = sanitize_output(data)
        assert "5309" not in result["note"]
        assert result["name"] == "Alice"

    def test_non_phone_strings_unchanged(self) -> None:
        assert sanitize_output("Hello world") == "Hello world"
        assert sanitize_output(42) == 42
        assert sanitize_output(None) is None

    def test_contains_phone_like(self) -> None:
        assert contains_phone_like("+15551234567")
        assert not contains_phone_like("Hello world")
        assert not contains_phone_like("abc123")


# ---------------------------------------------------------------------------
# Config route tests (Finding 1)
# ---------------------------------------------------------------------------


class TestConfigRouting:
    """Verify location_id comes from config.yaml, never from environment."""

    def test_load_location_id_reads_from_config(self) -> None:
        from unittest.mock import patch
        from plugins.team_duncan_contacts import _load_location_id

        fake_config = {
            "plugins": {
                "entries": {
                    "team_duncan_contacts": {
                        "settings": {"location_id": "loc-config-abc"}
                    }
                }
            }
        }
        with patch("hermes_cli.config.load_config", return_value=fake_config):
            result = _load_location_id()
        assert result == "loc-config-abc"

    def test_load_location_id_missing_returns_empty(self) -> None:
        from unittest.mock import patch
        from plugins.team_duncan_contacts import _load_location_id

        with patch("hermes_cli.config.load_config", return_value={}):
            result = _load_location_id()
        assert result == ""

    def test_load_location_id_ignores_env_var(self, monkeypatch) -> None:
        from unittest.mock import patch
        from plugins.team_duncan_contacts import _load_location_id

        monkeypatch.setenv("TEAM_DUNCAN_LOCATION_ID", "loc-env-only-xyz")
        with patch("hermes_cli.config.load_config", return_value={}):
            result = _load_location_id()
        assert result == "", (
            f"env var must not enable plugin; got {result!r}"
        )

    def test_register_env_only_does_not_register_tools(self, monkeypatch) -> None:
        """register() must not call ctx.register_tool when only env var is set."""
        from unittest.mock import patch, MagicMock
        from plugins.team_duncan_contacts import register

        monkeypatch.setenv("TEAM_DUNCAN_LOCATION_ID", "loc-env-only-xyz")
        fake_ctx = MagicMock()

        with patch("hermes_cli.config.load_config", return_value={}):
            register(fake_ctx)

        fake_ctx.register_tool.assert_not_called()

    def test_register_config_location_id_registers_tools(self, tmp_path) -> None:
        """register() calls ctx.register_tool twice when config has location_id."""
        from unittest.mock import patch, MagicMock
        from plugins.team_duncan_contacts import register
        from plugins.team_duncan_contacts.registry import ContactRegistry
        from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader as _FGR

        fake_config = {
            "plugins": {
                "entries": {
                    "team_duncan_contacts": {
                        "settings": {"location_id": "loc-config-123"}
                    }
                }
            }
        }
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        fake_ctx = MagicMock()

        with patch("hermes_cli.config.load_config", return_value=fake_config), \
             patch("hermes_constants.get_hermes_home", return_value=hermes_home), \
             patch.object(ContactRegistry, "startup_validate", return_value=None), \
             patch(
                 "plugins.team_duncan_contacts._build_live_ghl_reader",
                 return_value=_FGR([]),
             ):
            register(fake_ctx)

        assert fake_ctx.register_tool.call_count == 6
        registered_names = {
            call.kwargs.get("name") or call.args[0]
            for call in fake_ctx.register_tool.call_args_list
        }
        assert registered_names == {
            "prepare_activation",
            "confirm_activation",
            "list_pending_call_reviews",
            "prepare_call_log_ingest",
            "confirm_call_log_ingest",
            "accept_call_log_ingest_run",
        }


# ---------------------------------------------------------------------------
# Exception canary test (Finding 2)
# ---------------------------------------------------------------------------


class TestExceptionCanary:
    """A reader that raises an exception embedding a raw phone must not leak it."""

    # A distinct canary for this test so it cannot collide with CANARY_PHONE
    _EXC_CANARY = "+15550000001"

    def _make_raising_reader(self):
        canary = self._EXC_CANARY

        class _RaisingReader(FakeGhlReader):
            def get_contact_by_id(self, contact_id):
                raise RuntimeError(f"internal failure near {canary}")

            def search_contacts_by_name(self, query, location_id):
                raise RuntimeError(f"search failure near {canary}")

        return _RaisingReader([])

    def test_exception_canary_absent_from_handler_output(
        self, tmp_path: Path
    ) -> None:
        from plugins.team_duncan_contacts.tools import make_prepare_handler
        import logging

        reg = make_registry(tmp_path)
        reader = self._make_raising_reader()
        handler = make_prepare_handler(reg, reader)

        output = handler({"name_or_id": "Alice"})
        assert self._EXC_CANARY not in output, (
            f"Exception canary found in handler output: {output}"
        )

    def test_exception_canary_absent_from_logs(
        self, tmp_path: Path, caplog
    ) -> None:
        from plugins.team_duncan_contacts.tools import make_prepare_handler
        import logging

        reg = make_registry(tmp_path)
        reader = self._make_raising_reader()
        handler = make_prepare_handler(reg, reader)

        with caplog.at_level(logging.DEBUG):
            handler({"name_or_id": "Alice"})

        assert self._EXC_CANARY not in caplog.text, (
            f"Exception canary found in log output: {caplog.text}"
        )

    def test_exception_canary_absent_from_stdout_stderr(
        self, tmp_path: Path
    ) -> None:
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from plugins.team_duncan_contacts.tools import make_prepare_handler

        reg = make_registry(tmp_path)
        reader = self._make_raising_reader()
        handler = make_prepare_handler(reg, reader)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            handler({"name_or_id": "Alice"})

        assert self._EXC_CANARY not in stdout_buf.getvalue()
        assert self._EXC_CANARY not in stderr_buf.getvalue()

    def test_exception_handler_returns_generic_error(
        self, tmp_path: Path
    ) -> None:
        from plugins.team_duncan_contacts.tools import make_prepare_handler

        reg = make_registry(tmp_path)
        reader = self._make_raising_reader()
        handler = make_prepare_handler(reg, reader)

        output = handler({"name_or_id": "Alice"})
        result = json.loads(output)
        assert result["status"] == "error"
        assert "internal" in result["message"].lower()


# ---------------------------------------------------------------------------
# No em dash validator (Finding 3)
# ---------------------------------------------------------------------------


class TestNoEmDash:
    """Assert no em dash character (U+2014) appears in any OPS-72 source file."""

    _EM_DASH = chr(0x2014)

    @staticmethod
    def _ops72_files():
        root = Path(__file__).parents[3]
        return [
            root / "plugins" / "team_duncan_contacts" / "__init__.py",
            root / "plugins" / "team_duncan_contacts" / "ghl_reader.py",
            root / "plugins" / "team_duncan_contacts" / "plugin.yaml",
            root / "plugins" / "team_duncan_contacts" / "registry.py",
            root / "plugins" / "team_duncan_contacts" / "sanitizer.py",
            root / "plugins" / "team_duncan_contacts" / "tools.py",
            root / "tests" / "plugins" / "team_duncan_contacts" / "__init__.py",
            root / "tests" / "plugins" / "team_duncan_contacts" / "test_registry.py",
            root / "tests" / "plugins" / "test_team_duncan_contacts.py",
            root / "docs" / "operations" / "team-duncan-contacts.md",
        ]

    def test_no_em_dash_in_ops72_files(self) -> None:
        violations = []
        for path in self._ops72_files():
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if self._EM_DASH in text:
                lines = [
                    f"  line {i + 1}: {line.rstrip()}"
                    for i, line in enumerate(text.splitlines())
                    if self._EM_DASH in line
                ]
                violations.append(f"{path.name}:\n" + "\n".join(lines))
        assert not violations, (
            "Em dash (U+2014) found in OPS-72 files:\n" + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
