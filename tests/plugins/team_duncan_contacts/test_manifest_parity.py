"""Manifest parity regression test for team_duncan_contacts.

Guards against plugin.yaml's ``provides_tools`` drifting from the set of
agent-facing tools actually registered via ``ctx.register_tool`` in
``plugins/team_duncan_contacts/__init__.py``. Any future tool that is
registered but not declared (or declared but never registered) fails here
instead of surfacing as a silent manifest/runtime mismatch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from plugins.team_duncan_contacts.registry import ContactRegistry as _CR

_REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = _REPO_ROOT / "plugins" / "team_duncan_contacts" / "plugin.yaml"

LOCATION_ID = "loc-manifest-parity-test"


class _FakeGhlReader:
    def __init__(self, contacts):
        self._contacts = contacts


def _declared_tools() -> set[str]:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    return set(data.get("provides_tools", []))


def _registered_tools() -> set[str]:
    from plugins.team_duncan_contacts import register

    fake_config = {
        "plugins": {
            "entries": {
                "team_duncan_contacts": {
                    "settings": {"location_id": LOCATION_ID}
                }
            }
        }
    }

    with tempfile.TemporaryDirectory() as td:
        hermes_home = Path(td) / "hermes_home"
        hermes_home.mkdir()
        fake_ctx = MagicMock()
        with patch("hermes_cli.config.load_config", return_value=fake_config), \
             patch("hermes_constants.get_hermes_home", return_value=hermes_home), \
             patch.object(_CR, "startup_validate", return_value=None), \
             patch(
                 "plugins.team_duncan_contacts._build_live_ghl_reader",
                 return_value=_FakeGhlReader([]),
             ):
            register(fake_ctx)

        return {
            call.kwargs.get("name") or call.args[0]
            for call in fake_ctx.register_tool.call_args_list
        }


class TestManifestToolParity:
    def test_manifest_declares_exactly_the_registered_tools(self) -> None:
        declared = _declared_tools()
        registered = _registered_tools()
        assert declared == registered, (
            f"plugin.yaml provides_tools is out of sync with register(): "
            f"missing from manifest={registered - declared}, "
            f"missing from register()={declared - registered}"
        )

    def test_required_tools_are_declared_and_registered(self) -> None:
        required = {
            "set_imessage_activation",
            "prepare_plaud_summary_run",
            "confirm_plaud_summary_run",
        }
        declared = _declared_tools()
        registered = _registered_tools()
        assert required <= declared
        assert required <= registered
