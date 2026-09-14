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

# Module-level ledger instance, set by register() at plugin load time.
# Internal Python API only; not agent-facing.
activity_ledger = None


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
    """Construct the production GHL contact reader for Team Duncan.

    Goes through ``tools.ghl_client.scoped_client()``, the same reusable
    boundary contact writes go through: this raises ``ScopeViolationError``
    before any GHL transport request if *location_id* (from config.yaml)
    does not match the location the ``team_duncan`` account is pinned to, so
    a misconfigured config can never reach the network under this account.
    """
    from hermes_constants import get_hermes_home
    from tools.ghl_client import TEAM_DUNCAN_ACCOUNT_KEY, scoped_client

    from .ghl_reader import ScopedGhlReader

    client = scoped_client(
        TEAM_DUNCAN_ACCOUNT_KEY, location_id, hermes_home=get_hermes_home()
    )
    return ScopedGhlReader(client)


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

    from .activity_ledger import ActivityLedger
    import plugins.team_duncan_contacts as _self

    data_dir = Path(hermes_home) / "plugin-data" / "team_duncan_contacts"
    data_dir.mkdir(parents=True, exist_ok=True)
    _self.activity_ledger = ActivityLedger(data_dir / "activity.db", registry)
    log.info(
        "team_duncan_contacts: activity ledger initialised at %s.",
        data_dir / "activity.db",
    )

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
