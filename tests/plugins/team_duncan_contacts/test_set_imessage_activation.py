"""Tests for OPS-75: set_imessage_activation.

A reversible on/off switch for the local iMessage Note lane only, driven by
plain-language commands like "activate [name]'s iMessages". All tests use
fakes and temporary directories: no live GHL calls, no real credentials.

Test categories:
 1. First activation (absent contact, action=activate).
 2. Active idempotency (activate on active).
 3. Paused resume preserving the original cutoff (activate on paused).
 4. Active pause (deactivate on active).
 5. Paused idempotency (deactivate on paused).
 6. Retired rejection, fail closed (activate and deactivate on retired).
 7. Absent deactivate (deactivate on a never-activated contact).
 8. Exact contact ID lookup.
 9. Case-insensitive exact display-name lookup.
10. Ambiguous name, no state change.
11. Raw-input rejection (phone/email/Apple handle), fails closed before any
    state or network change.
12. GHL reader failure surfaces a generic error with no leak.
13. No DND/tag/write-client usage anywhere on this path.
14. No raw handle in any output.
15. Atomic state/history: append-only transitions, activated_at immutable.
16. Tool registration and schema shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.registry import ContactRegistry
from plugins.team_duncan_contacts.tools import (
    SET_IMESSAGE_ACTIVATION_SCHEMA,
    make_set_imessage_activation_handler,
)

LOCATION_ID = "loc-team-duncan-123"

# Synthetic canary phone: must never appear outside the registry boundary.
CANARY_PHONE = "+15551239999"

CONTACT_ALICE = {
    "id": "ghl-alice-001",
    "locationId": LOCATION_ID,
    "firstName": "Alice",
    "lastName": "Anderson",
    "phone": CANARY_PHONE,
    "email": "alice@example.com",
}

CONTACT_JOHN_1 = {
    "id": "ghl-john-001",
    "locationId": LOCATION_ID,
    "firstName": "John",
    "lastName": "Smith",
    "phone": "+15551110001",
}

CONTACT_JOHN_2 = {
    "id": "ghl-john-002",
    "locationId": LOCATION_ID,
    "firstName": "John",
    "lastName": "Smith",
    "phone": "+15551110002",
}


def make_clock(ts: datetime):
    box = [ts]

    def _clock() -> datetime:
        return box[0]

    def _advance(seconds: float) -> None:
        box[0] = box[0] + timedelta(seconds=seconds)

    _clock.advance = _advance
    return _clock


def make_registry(tmp_path: Path, clock=None, location_id: str = LOCATION_ID) -> ContactRegistry:
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    return ContactRegistry(hermes_home, team_duncan_location_id=location_id, clock=clock)


def _load_state(reg: ContactRegistry) -> dict:
    return json.loads(reg._state_path.read_text(encoding="utf-8"))


class _RaisingReader(FakeGhlReader):
    """A reader whose every read call raises, embedding a canary."""

    CANARY = "+15550000002"

    def __init__(self) -> None:
        super().__init__([])

    def get_contact_by_id(self, contact_id):
        raise RuntimeError(f"internal failure near {self.CANARY}")

    def search_contacts_by_name(self, query, location_id):
        raise RuntimeError(f"search failure near {self.CANARY}")


# ---------------------------------------------------------------------------
# 1. First activation
# ---------------------------------------------------------------------------


class TestFirstActivation:
    def test_absent_contact_activates(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        result = reg.set_imessage_activation("activate", "Alice Anderson", reader)

        assert result.status == "activated"
        assert result.action == "activate"
        assert result.contact_id == CONTACT_ALICE["id"]
        assert result.location_id == LOCATION_ID
        assert result.lifecycle_state == "active"
        assert result.activated_at is not None

        state = _load_state(reg)
        assert state["contacts"][CONTACT_ALICE["id"]]["state"] == "active"

    def test_absent_contact_activation_creates_history(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reg.set_imessage_activation("activate", "Alice Anderson", FakeGhlReader([CONTACT_ALICE]))
        state = _load_state(reg)
        transitions = [
            h["transition"] for h in state["contacts"][CONTACT_ALICE["id"]]["history"]
        ]
        assert transitions == ["activated"]

    def test_no_ghl_contact_returns_contact_creation_required(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        result = reg.set_imessage_activation("activate", "Ghost Person", FakeGhlReader([CONTACT_ALICE]))
        assert result.status == "contact_creation_required"
        state_path = reg._state_path
        assert not state_path.exists() or _load_state(reg)["contacts"] == {}


# ---------------------------------------------------------------------------
# 2. Active idempotency
# ---------------------------------------------------------------------------


class TestActiveIdempotency:
    def test_activate_on_active_is_idempotent(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        first = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        assert first.status == "activated"

        second = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        assert second.status == "already_active"
        assert second.activated_at == first.activated_at
        assert second.lifecycle_state == "active"


# ---------------------------------------------------------------------------
# 3. Paused resume preserves cutoff
# ---------------------------------------------------------------------------


class TestPausedResume:
    def test_resume_preserves_original_cutoff(self, tmp_path: Path) -> None:
        clock = make_clock(datetime(2024, 5, 1, 9, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)
        reader = FakeGhlReader([CONTACT_ALICE])

        activated = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        original_cutoff = activated.activated_at

        clock.advance(3600)
        paused = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert paused.status == "deactivated"

        clock.advance(86400)
        resumed = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        assert resumed.status == "resumed"
        assert resumed.lifecycle_state == "active"
        assert resumed.activated_at == original_cutoff


# ---------------------------------------------------------------------------
# 4. Active pause
# ---------------------------------------------------------------------------


class TestActivePause:
    def test_deactivate_on_active_pauses(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)

        result = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert result.status == "deactivated"
        assert result.lifecycle_state == "paused"

        state = _load_state(reg)
        assert state["contacts"][CONTACT_ALICE["id"]]["state"] == "paused"


# ---------------------------------------------------------------------------
# 5. Paused idempotency
# ---------------------------------------------------------------------------


class TestPausedIdempotency:
    def test_deactivate_on_paused_is_idempotent(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)
        first = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert first.status == "deactivated"

        second = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert second.status == "already_paused"
        assert second.lifecycle_state == "paused"

        state = _load_state(reg)
        transitions = [
            h["transition"] for h in state["contacts"][CONTACT_ALICE["id"]]["history"]
        ]
        # No extra "paused" transition was appended for the idempotent call.
        assert transitions == ["activated", "paused"]


# ---------------------------------------------------------------------------
# 6. Retired rejection
# ---------------------------------------------------------------------------


class TestRetiredRejection:
    def test_activate_on_retired_fails_closed(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "test retire")

        state_before = _load_state(reg)
        result = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        assert result.status == "retired_contact"
        state_after = _load_state(reg)
        assert state_before["contacts"] == state_after["contacts"]

    def test_deactivate_on_retired_fails_closed(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)
        reg.retire_contact(CONTACT_ALICE["id"], "clay", "test retire")

        state_before = _load_state(reg)
        result = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert result.status == "retired_contact"
        state_after = _load_state(reg)
        assert state_before["contacts"] == state_after["contacts"]
        # The terminal "retired" state was never overwritten by pause_contact.
        assert state_after["contacts"][CONTACT_ALICE["id"]]["state"] == "retired"


# ---------------------------------------------------------------------------
# 7. Absent deactivate
# ---------------------------------------------------------------------------


class TestAbsentDeactivate:
    def test_deactivate_never_activated_contact(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        result = reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        assert result.status == "not_activated"
        assert result.action == "deactivate"

        state_path = reg._state_path
        assert not state_path.exists() or _load_state(reg)["contacts"] == {}


# ---------------------------------------------------------------------------
# 8. Exact contact ID lookup
# ---------------------------------------------------------------------------


class TestExactIdLookup:
    def test_resolves_by_exact_contact_id(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)

        result = reg.set_imessage_activation("deactivate", CONTACT_ALICE["id"], reader)
        assert result.status == "deactivated"
        assert result.contact_id == CONTACT_ALICE["id"]


# ---------------------------------------------------------------------------
# 9. Case-insensitive exact name lookup
# ---------------------------------------------------------------------------


class TestCaseInsensitiveName:
    def test_resolves_by_case_insensitive_display_name(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)

        result = reg.set_imessage_activation("deactivate", "ALICE anderson", reader)
        assert result.status == "deactivated"
        assert result.contact_id == CONTACT_ALICE["id"]


# ---------------------------------------------------------------------------
# 10. Ambiguity
# ---------------------------------------------------------------------------


class TestAmbiguity:
    def test_ambiguous_name_fails_with_no_state_change(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reg.set_imessage_activation(
            "activate", CONTACT_JOHN_1["id"], FakeGhlReader([CONTACT_JOHN_1])
        )
        reg.set_imessage_activation(
            "activate", CONTACT_JOHN_2["id"], FakeGhlReader([CONTACT_JOHN_2])
        )

        state_before = _load_state(reg)
        result = reg.set_imessage_activation(
            "deactivate", "John Smith", FakeGhlReader([CONTACT_JOHN_1, CONTACT_JOHN_2])
        )
        assert result.status == "ambiguous"
        assert result.contact_id is None
        state_after = _load_state(reg)
        assert state_before["contacts"] == state_after["contacts"]


# ---------------------------------------------------------------------------
# 11. Raw-input rejection
# ---------------------------------------------------------------------------


class TestRawInputRejection:
    @pytest.mark.parametrize(
        "bad_input",
        [
            "+15558675309",
            "alice@example.com",
            "tel:+15551234567",
            "imessage:alice@example.com",
        ],
    )
    def test_raw_handle_rejected_before_any_change(self, tmp_path: Path, bad_input: str) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        result = reg.set_imessage_activation("activate", bad_input, reader)
        assert result.status == "invalid_input"
        state_path = reg._state_path
        assert not state_path.exists() or _load_state(reg)["contacts"] == {}

    def test_raw_handle_rejected_even_for_existing_contact(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)

        state_before = _load_state(reg)
        result = reg.set_imessage_activation("deactivate", CANARY_PHONE, reader)
        assert result.status == "invalid_input"
        state_after = _load_state(reg)
        assert state_before["contacts"] == state_after["contacts"]


# ---------------------------------------------------------------------------
# 12. GHL reader failure
# ---------------------------------------------------------------------------


class TestGhlReaderFailure:
    def test_reader_failure_returns_generic_error_no_leak(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        reg = ContactRegistry(hermes_home, team_duncan_location_id=LOCATION_ID)
        handler = make_set_imessage_activation_handler(reg, _RaisingReader())

        output = handler({"action": "activate", "name_or_id": "Alice Anderson"})
        assert _RaisingReader.CANARY not in output
        result = json.loads(output)
        assert result["status"] == "error"

    def test_reader_failure_absent_from_logs(self, tmp_path: Path, caplog) -> None:
        import logging

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        reg = ContactRegistry(hermes_home, team_duncan_location_id=LOCATION_ID)
        handler = make_set_imessage_activation_handler(reg, _RaisingReader())

        with caplog.at_level(logging.DEBUG):
            handler({"action": "activate", "name_or_id": "Alice Anderson"})
        assert _RaisingReader.CANARY not in caplog.text


# ---------------------------------------------------------------------------
# 13. No DND / tag / write-client usage
# ---------------------------------------------------------------------------


class TestNoGhlMutation:
    def test_full_cycle_never_instantiates_write_client(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        mock_write_client = MagicMock()
        with patch("tools.ghl_client.GoHighLevelWriteClient", mock_write_client):
            reg = make_registry(tmp_path)
            reader = FakeGhlReader([CONTACT_ALICE])
            reg.set_imessage_activation("activate", "Alice Anderson", reader)
            reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
            reg.set_imessage_activation("activate", "Alice Anderson", reader)

        mock_write_client.assert_not_called()

    def test_no_dnd_or_tag_mutation_call_in_source(self) -> None:
        """Static check: the activation-control source path never calls a
        DND or tag mutation method, so there is no code path to a GHL
        tag/DND write. (Prose *mentioning* DND/tags to document their
        absence, e.g. in docstrings, is expected and fine.)"""
        import plugins.team_duncan_contacts.registry as registry_mod
        import plugins.team_duncan_contacts.tools as tools_mod

        for mod in (registry_mod, tools_mod):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert "set_dnd" not in src.lower()
            assert "add_tag" not in src.lower()
            assert "addtag" not in src.lower()
            assert "update_contact" not in src.lower()

    def test_ghl_reader_has_no_write_methods_available_to_the_tool(self) -> None:
        reader = FakeGhlReader([CONTACT_ALICE])
        for forbidden in ("update_contact", "add_tag", "set_dnd", "create_note"):
            assert not hasattr(reader, forbidden)


# ---------------------------------------------------------------------------
# 14. No raw handle in output
# ---------------------------------------------------------------------------


class TestNoRawOutput:
    def test_canary_absent_through_full_lifecycle(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        reg = ContactRegistry(hermes_home, team_duncan_location_id=LOCATION_ID)
        reader = FakeGhlReader([CONTACT_ALICE])
        handler = make_set_imessage_activation_handler(reg, reader)

        outputs = [
            handler({"action": "activate", "name_or_id": "Alice Anderson"}),
            handler({"action": "deactivate", "name_or_id": "Alice Anderson"}),
            handler({"action": "activate", "name_or_id": "Alice Anderson"}),
        ]
        for out in outputs:
            assert CANARY_PHONE not in out

        state_text = reg._state_path.read_text(encoding="utf-8")
        assert CANARY_PHONE not in state_text


# ---------------------------------------------------------------------------
# 15. Atomic state / history
# ---------------------------------------------------------------------------


class TestAtomicStateHistory:
    def test_history_appends_and_activated_at_immutable(self, tmp_path: Path) -> None:
        clock = make_clock(datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc))
        reg = make_registry(tmp_path, clock=clock)
        reader = FakeGhlReader([CONTACT_ALICE])

        activated = reg.set_imessage_activation("activate", "Alice Anderson", reader)
        original_ts = activated.activated_at

        clock.advance(60)
        reg.set_imessage_activation("deactivate", "Alice Anderson", reader)
        clock.advance(60)
        reg.set_imessage_activation("activate", "Alice Anderson", reader)

        state = _load_state(reg)
        contact = state["contacts"][CONTACT_ALICE["id"]]
        assert contact["activated_at"] == original_ts
        transitions = [h["transition"] for h in contact["history"]]
        assert transitions == ["activated", "paused", "active"]

    def test_state_file_written_atomically_via_tmp_replace(self, tmp_path: Path) -> None:
        reg = make_registry(tmp_path)
        reader = FakeGhlReader([CONTACT_ALICE])
        reg.set_imessage_activation("activate", "Alice Anderson", reader)
        tmp_path_candidate = reg._state_path.with_suffix(".tmp")
        # The temp file must never be left behind after a successful write.
        assert not tmp_path_candidate.exists()
        assert reg._state_path.exists()


# ---------------------------------------------------------------------------
# 16. Tool registration / schema
# ---------------------------------------------------------------------------


class TestToolSchema:
    def test_schema_has_action_enum_and_name_or_id_only(self) -> None:
        params = SET_IMESSAGE_ACTIVATION_SCHEMA["function"]["parameters"]
        props = params.get("properties", {})
        assert set(props.keys()) == {"action", "name_or_id"}
        assert props["action"]["enum"] == ["activate", "deactivate"]
        assert set(params["required"]) == {"action", "name_or_id"}

    def test_schema_has_no_phone_or_handle_fields(self) -> None:
        params = SET_IMESSAGE_ACTIVATION_SCHEMA["function"]["parameters"]
        props = params.get("properties", {})
        assert "phone" not in props
        assert "email" not in props
        assert "handle" not in props

    def test_handler_rejects_invalid_action(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        reg = ContactRegistry(hermes_home, team_duncan_location_id=LOCATION_ID)
        reader = FakeGhlReader([CONTACT_ALICE])
        handler = make_set_imessage_activation_handler(reg, reader)

        result = json.loads(handler({"action": "retire", "name_or_id": "Alice Anderson"}))
        assert result["status"] == "invalid_input"

    def test_registered_in_plugin_init(self) -> None:
        from unittest.mock import MagicMock, patch

        from plugins.team_duncan_contacts import register
        from plugins.team_duncan_contacts.registry import ContactRegistry as _CR

        fake_config = {
            "plugins": {
                "entries": {
                    "team_duncan_contacts": {"settings": {"location_id": "loc-config-999"}}
                }
            }
        }
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            hermes_home = Path(td) / "hermes_home"
            hermes_home.mkdir()
            fake_ctx = MagicMock()
            with patch("hermes_cli.config.load_config", return_value=fake_config), \
                 patch("hermes_constants.get_hermes_home", return_value=hermes_home), \
                 patch.object(_CR, "startup_validate", return_value=None), \
                 patch(
                     "plugins.team_duncan_contacts._build_live_ghl_reader",
                     return_value=FakeGhlReader([]),
                 ):
                register(fake_ctx)

            registered_names = {
                call.kwargs.get("name") or call.args[0]
                for call in fake_ctx.register_tool.call_args_list
            }
            assert "set_imessage_activation" in registered_names
