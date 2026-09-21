"""Tests that the OPS-18 ingestion runner factory wires the Desk production
transport (`LiveDeskTransport`), keeps Plaud unconfigured pending OPS-110,
and -- per Clay's source-isolation correction -- enables only `desk_call`
in the runner's explicit enabled-source contract, so Plaud is never
attempted at all (not merely tolerated on failure). No live transport is
ever invoked here -- only the wired object identities and the
enabled-source configuration are inspected, and every attempted direct
Plaud call is asserted to fail closed, in-process, with no I/O of any kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import plugins.team_duncan_contacts as team_duncan_contacts
from plugins.team_duncan_contacts.collectors.call_history_collector import LiveDeskTransport
from plugins.team_duncan_contacts.ingestion_state_db import SOURCE_DESK_CALL, SOURCE_PLAUD
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID


def test_factory_wires_live_desk_transport_and_unconfigured_plaud(tmp_path: Path) -> None:
    registry = ContactRegistry(
        tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID
    )
    factory = team_duncan_contacts._build_ingestion_runner_factory(
        tmp_path / "hermes_home", registry
    )

    runner, _state_db = factory()

    assert isinstance(runner._desk._transport, LiveDeskTransport)
    assert isinstance(runner._plaud._transport, team_duncan_contacts._UnconfiguredTransport)

    with pytest.raises(team_duncan_contacts._LiveTransportNotConfiguredError):
        runner._plaud._transport.get_deployment_boundary()
    with pytest.raises(team_duncan_contacts._LiveTransportNotConfiguredError):
        runner._plaud._transport.fetch_records_since(None)


def test_factory_enables_only_desk_source(tmp_path: Path) -> None:
    registry = ContactRegistry(
        tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID
    )
    factory = team_duncan_contacts._build_ingestion_runner_factory(
        tmp_path / "hermes_home", registry
    )

    runner, _state_db = factory()

    assert runner._enabled_sources == frozenset({SOURCE_DESK_CALL})
    assert SOURCE_PLAUD not in runner._enabled_sources


def test_module_enabled_sources_constant_is_desk_only() -> None:
    """Single source of truth backing both the runner factory and
    prepare_call_log_ingest's reported `sources` -- must agree with itself
    by construction, but pin the value directly so a future edit to either
    call site is caught here too."""
    assert team_duncan_contacts._ENABLED_SOURCES == (SOURCE_DESK_CALL,)
