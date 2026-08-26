"""team_duncan_contacts plugin: Clay-approved contact activation registry.

Registers two agent-facing tools: prepare_activation and confirm_activation.
See plugins/team_duncan_contacts/registry.py for core invariants.

Installation: add ``team_duncan_contacts`` to ``plugins.enabled`` in config.yaml.
Do NOT enable in the live profile until the post-build review steps are complete.

Required config (in config.yaml):
  plugins:
    entries:
      team_duncan_contacts:
        settings:
          location_id: "<GoHighLevel location ID for Team Duncan>"
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PLUGIN_NAME = "team_duncan_contacts"


def _load_location_id() -> str:
    """Read the Team Duncan location ID from config.yaml.

    Config path: plugins.entries.team_duncan_contacts.settings.location_id
    Returns empty string if the setting is absent or config cannot be loaded.
    Never reads from environment variables.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        plugins_cfg = cfg.get("plugins") if isinstance(cfg, dict) else None
        entries = plugins_cfg.get("entries") if isinstance(plugins_cfg, dict) else None
        plugin_cfg = (
            entries.get("team_duncan_contacts") if isinstance(entries, dict) else None
        )
        settings = plugin_cfg.get("settings") if isinstance(plugin_cfg, dict) else None
        val = settings.get("location_id") if isinstance(settings, dict) else None
        return (val or "").strip()
    except Exception as exc:
        log.debug(
            "team_duncan_contacts: could not load config [%s]", type(exc).__name__
        )
        return ""


def _build_live_ghl_reader(location_id: str):
    """Construct the production GHL contact reader for Team Duncan."""
    from .ghl_reader import GhlContactReader
    from hermes_constants import get_hermes_home
    from tools.sync_json_http import urllib_request, build_url, request_json
    from tools.ghl_client import GHL_API_BASE_URL, GHL_API_VERSION, api_key_from_hermes_env

    class _LiveReader(GhlContactReader):
        def __init__(self):
            self._api_key = None
            self._hermes_home = get_hermes_home()

        @property
        def _key(self) -> str:
            if self._api_key is None:
                self._api_key = api_key_from_hermes_env(
                    "team_duncan", hermes_home=self._hermes_home
                )
            return self._api_key

        def _headers(self) -> dict:
            return {
                "Authorization": f"Bearer {self._key}",
                "Version": GHL_API_VERSION,
                "Accept": "application/json",
                "User-Agent": "Hermes-Agent/team-duncan-contacts-reader",
            }

        def get_contact_by_id(self, contact_id: str) -> dict | None:
            try:
                result = request_json(
                    urllib_request,
                    "GET",
                    build_url(GHL_API_BASE_URL, f"/contacts/{contact_id}", None),
                    headers=self._headers(),
                    timeout=20.0,
                    max_retries=2,
                    sleep=__import__("time").sleep,
                )
                c = result.get("contact") if isinstance(result, dict) else result
                return c if isinstance(c, dict) else None
            except Exception:
                return None

        def search_contacts_by_name(
            self, query: str, location_id: str
        ) -> list[dict]:
            try:
                result = request_json(
                    urllib_request,
                    "GET",
                    build_url(
                        GHL_API_BASE_URL,
                        "/contacts/search",
                        {"locationId": location_id, "query": query, "limit": "20"},
                    ),
                    headers=self._headers(),
                    timeout=20.0,
                    max_retries=2,
                    sleep=__import__("time").sleep,
                )
                contacts = result.get("contacts") if isinstance(result, dict) else None
                return contacts if isinstance(contacts, list) else []
            except Exception:
                return []

    return _LiveReader()


def register(ctx) -> None:
    """Plugin entry point: called by the Hermes plugin loader."""
    location_id = _load_location_id()
    if not location_id:
        log.warning(
            "team_duncan_contacts: location_id is not set in config.yaml at "
            "plugins.entries.team_duncan_contacts.settings.location_id. "
            "Plugin tools will not be registered."
        )
        return

    from hermes_constants import get_hermes_home
    from .registry import ContactRegistry
    from .tools import (
        PREPARE_ACTIVATION_SCHEMA,
        CONFIRM_ACTIVATION_SCHEMA,
        make_prepare_handler,
        make_confirm_handler,
    )

    hermes_home = get_hermes_home()
    registry = ContactRegistry(hermes_home, team_duncan_location_id=location_id)

    try:
        registry.startup_validate()
    except Exception as exc:
        log.error(
            "team_duncan_contacts: startup validation failed [%s]. "
            "Plugin tools will not be registered.",
            type(exc).__name__,
        )
        return

    try:
        ghl_reader = _build_live_ghl_reader(location_id)
    except Exception as exc:
        log.error(
            "team_duncan_contacts: failed to build GHL reader [%s]. "
            "Plugin tools will not be registered.",
            type(exc).__name__,
        )
        return

    prepare_handler = make_prepare_handler(registry, ghl_reader)
    confirm_handler = make_confirm_handler(registry)

    ctx.register_tool(
        name="prepare_activation",
        toolset=_PLUGIN_NAME,
        schema=PREPARE_ACTIVATION_SCHEMA,
        handler=prepare_handler,
        description=PREPARE_ACTIVATION_SCHEMA["function"]["description"],
    )

    ctx.register_tool(
        name="confirm_activation",
        toolset=_PLUGIN_NAME,
        schema=CONFIRM_ACTIVATION_SCHEMA,
        handler=confirm_handler,
        description=CONFIRM_ACTIVATION_SCHEMA["function"]["description"],
    )

    log.info(
        "team_duncan_contacts: registered prepare_activation and confirm_activation "
        "tools for location %s.",
        location_id,
    )
