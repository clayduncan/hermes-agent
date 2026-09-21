"""team_duncan_contacts plugin: Clay-approved contact activation registry.

Registers agent-facing tools: prepare_activation, confirm_activation, and
(OPS-18) list_pending_call_reviews, prepare_call_log_ingest,
confirm_call_log_ingest, accept_call_log_ingest_run.
See plugins/team_duncan_contacts/registry.py for core invariants.

Installation: add ``team_duncan_contacts`` to ``plugins.enabled`` in config.yaml.
Do NOT enable in the live profile until the post-build review steps are complete.

Required config (in config.yaml):
  plugins:
    entries:
      team_duncan_contacts:
        settings:
          location_id: "<GoHighLevel location ID for Team Duncan>"

OPS-18 note: registering these ingestion tools does not enable live
ingestion by itself. The Desk production transport (`LiveDeskTransport`) is
now wired for confirm_call_log_ingest's `desk_call` source: a fixed-path,
fixed-identity, one-shot SSH read is possible once Clay accepts this build
and enables the plugin. Plaud stays out of every run entirely, by explicit
fixed configuration (`_ENABLED_SOURCES` below), pending OPS-110: the
IngestionRunner never calls into Plaud at all for a disabled source, so
there is no cursor read, initialization, fetch, or error to tolerate. The
PlaudCollector is still constructed over `_UnconfiguredTransport` as
defense in depth, so even a future wiring mistake fails safely and locally
with no socket ever opened. The manual-run gate (prepare/confirm/accept,
single-use 300s tokens, cron rejection) still governs every source read:
prepare_call_log_ingest never reads a source and truthfully reports only
the enabled sources, and only confirm_call_log_ingest, after consuming its
single-use token, can invoke the Desk transport -- never a scheduler, never
a background process. The Pending Call Reviews surface is otherwise fully
functional against local state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .ingestion_state_db import SOURCE_DESK_CALL

log = logging.getLogger(__name__)

_PLUGIN_NAME = "team_duncan_contacts"

# Module-level ledger instance, set by register() at plugin load time.
# Internal Python API only; not agent-facing.
activity_ledger = None

# Fixed internal configuration, not agent/model input: which ingestion
# sources this production build runs. Plaud stays disabled until OPS-110 --
# not merely fail-closed on read, but never attempted at all. Single source
# of truth for both the IngestionRunner and prepare_call_log_ingest's
# reported `sources`, so they cannot drift from each other.
_ENABLED_SOURCES: tuple[str, ...] = (SOURCE_DESK_CALL,)


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


class _LiveTransportNotConfiguredError(RuntimeError):
    """Raised by every method of `_UnconfiguredTransport`. No live Plaud/Desk
    transport is wired into this build; confirm_call_log_ingest's own fetch
    handling turns this into a clean per-source error, never a live call."""


class _UnconfiguredTransport:
    """Stand-in for a live source transport. Every call raises immediately,
    in-process, with no I/O of any kind -- guaranteeing this build can never
    reach the Desk or Plaud even if ingestion is triggered."""

    def __getattr__(self, name: str):
        def _raise(*_args, **_kwargs):
            raise _LiveTransportNotConfiguredError(
                "No live transport is configured for this source in this build. "
                "Live ingestion requires Clay acceptance and separate transport "
                "wiring (OPS-41/OPS-18 live activation), not part of this build."
            )

        return _raise


class _UnconfiguredNotifier:
    """Always reports delivery failure; never sends anything anywhere."""

    def send(self, payload) -> bool:  # noqa: ANN001 - matches Notifier protocol
        return False


def _build_note_mirror_ghl_client(hermes_home: Path):
    """Construct the audited GHL writer the note mirror uses to create notes.

    Goes through ``tools.ghl_client.scoped_client()`` with the fixed Team
    Duncan constants directly (not a config-derived value): this fails
    before any network attempt on a scope mismatch, and since both
    arguments are the module's own fixed binding, that failure is a static
    impossibility here rather than something config.yaml could trigger.
    """
    from tools.ghl_client import TEAM_DUNCAN_ACCOUNT_KEY, TEAM_DUNCAN_LOCATION_ID, scoped_client

    return scoped_client(TEAM_DUNCAN_ACCOUNT_KEY, TEAM_DUNCAN_LOCATION_ID, hermes_home=hermes_home)


def _build_ingestion_runner_factory(hermes_home: Path, registry):
    """Return a zero-arg factory producing a fresh (IngestionRunner, state_db)
    pair. Deferred construction keeps plugin load itself free of any DB or
    transport work beyond what prepare/list already need."""

    def _factory():
        from .collectors.call_history_collector import CallHistoryCollector, LiveDeskTransport
        from .collectors.plaud_collector import PlaudCollector
        from .ingestion_state_db import IngestionStateDb, load_or_create_identity_key
        from .ingestion_runner import IngestionRunner
        from .note_mirror import NoteMirror
        from .notifications import TelegramPrimaryEmailFallbackNotifier

        data_dir = Path(hermes_home) / "plugin-data" / "team_duncan_contacts"
        data_dir.mkdir(parents=True, exist_ok=True)
        state_db = IngestionStateDb(data_dir / "ingestion_state.db")
        identity_key = load_or_create_identity_key(data_dir)

        # Desk: production transport, fixed identity/target, manual-run gate
        # only (see confirm_call_log_ingest). Plaud: disabled via
        # enabled_sources below, pending OPS-110 -- _run_plaud is never
        # called, so this collector is never read from even though it's
        # constructed fail-closed as defense in depth.
        plaud_collector = PlaudCollector(_UnconfiguredTransport())
        desk_collector = CallHistoryCollector(LiveDeskTransport(), identity_key)
        notifier = TelegramPrimaryEmailFallbackNotifier(
            _UnconfiguredNotifier(), _UnconfiguredNotifier()
        )
        note_mirror = NoteMirror(_build_note_mirror_ghl_client(hermes_home), state_db)

        runner = IngestionRunner(
            registry=registry,
            activity_ledger=activity_ledger,
            state_db=state_db,
            plaud_collector=plaud_collector,
            desk_collector=desk_collector,
            notifier=notifier,
            enabled_sources=frozenset(_ENABLED_SOURCES),
            note_mirror=note_mirror,
        )
        return runner, state_db

    return _factory


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

    # --- OPS-18: Pending Call Reviews + manual-run ingestion gate ---
    from .ingestion_state_db import IngestionStateDb
    from .tools import (
        ACCEPT_CALL_LOG_INGEST_RUN_SCHEMA,
        CONFIRM_CALL_LOG_INGEST_SCHEMA,
        LIST_PENDING_CALL_REVIEWS_SCHEMA,
        PREPARE_CALL_LOG_INGEST_SCHEMA,
        make_accept_call_log_ingest_run_handler,
        make_confirm_call_log_ingest_handler,
        make_list_pending_call_reviews_handler,
        make_prepare_call_log_ingest_handler,
    )

    ingestion_state_db = IngestionStateDb(data_dir / "ingestion_state.db")
    runner_factory = _build_ingestion_runner_factory(hermes_home, registry)

    ctx.register_tool(
        name="list_pending_call_reviews",
        toolset=_PLUGIN_NAME,
        schema=LIST_PENDING_CALL_REVIEWS_SCHEMA,
        handler=make_list_pending_call_reviews_handler(ingestion_state_db),
        description=LIST_PENDING_CALL_REVIEWS_SCHEMA["function"]["description"],
    )
    ctx.register_tool(
        name="prepare_call_log_ingest",
        toolset=_PLUGIN_NAME,
        schema=PREPARE_CALL_LOG_INGEST_SCHEMA,
        handler=make_prepare_call_log_ingest_handler(
            ingestion_state_db, enabled_sources=_ENABLED_SOURCES
        ),
        description=PREPARE_CALL_LOG_INGEST_SCHEMA["function"]["description"],
    )
    ctx.register_tool(
        name="confirm_call_log_ingest",
        toolset=_PLUGIN_NAME,
        schema=CONFIRM_CALL_LOG_INGEST_SCHEMA,
        handler=make_confirm_call_log_ingest_handler(runner_factory),
        description=CONFIRM_CALL_LOG_INGEST_SCHEMA["function"]["description"],
    )
    ctx.register_tool(
        name="accept_call_log_ingest_run",
        toolset=_PLUGIN_NAME,
        schema=ACCEPT_CALL_LOG_INGEST_RUN_SCHEMA,
        handler=make_accept_call_log_ingest_run_handler(ingestion_state_db),
        description=ACCEPT_CALL_LOG_INGEST_RUN_SCHEMA["function"]["description"],
    )

    log.info(
        "team_duncan_contacts: registered OPS-18 Pending Call Reviews and "
        "call-log ingestion manual-run gate tools. Desk production transport "
        "configured (fixed identity/target, manual-run gate only, no "
        "scheduler). Enabled ingestion sources: %s. Plaud is disabled "
        "pending OPS-110 and is never attempted this run.",
        list(_ENABLED_SOURCES),
    )
