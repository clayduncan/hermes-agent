"""Plugin-level regression test for the Team Duncan scoped-client boundary (OPS-104).

register() must refuse to activate the plugin's tools when config.yaml points
Team Duncan at a location other than the one it is pinned to, and it must do
so without ever attempting a GHL request: the failure happens inside
tools.ghl_client.scoped_client() at construction time, before any lookup,
audit authorization, or audit intent could occur.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID


def _fake_config(location_id: str) -> dict:
    return {
        "plugins": {
            "entries": {
                "team_duncan_contacts": {"settings": {"location_id": location_id}}
            }
        }
    }


class TestRegisterEnforcesTheFixedLocation:
    def test_wrong_configured_location_registers_no_tools(self, tmp_path: Path) -> None:
        from plugins.team_duncan_contacts import register
        from plugins.team_duncan_contacts.registry import ContactRegistry

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        fake_ctx = MagicMock()

        with patch(
            "hermes_cli.config.load_config",
            return_value=_fake_config("not-the-real-team-duncan-location"),
        ), patch(
            "hermes_constants.get_hermes_home", return_value=hermes_home
        ), patch.object(ContactRegistry, "startup_validate", return_value=None):
            register(fake_ctx)

        fake_ctx.register_tool.assert_not_called()

    def test_correct_configured_location_is_accepted_end_to_end(
        self, tmp_path: Path
    ) -> None:
        from plugins.team_duncan_contacts import register
        from plugins.team_duncan_contacts.registry import ContactRegistry

        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()
        fake_ctx = MagicMock()

        with patch(
            "hermes_cli.config.load_config",
            return_value=_fake_config(TEAM_DUNCAN_LOCATION_ID),
        ), patch(
            "hermes_constants.get_hermes_home", return_value=hermes_home
        ), patch.object(ContactRegistry, "startup_validate", return_value=None):
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
