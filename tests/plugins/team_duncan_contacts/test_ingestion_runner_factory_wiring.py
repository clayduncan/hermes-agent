"""Tests that the OPS-18 ingestion runner factory wires the Desk production
transport (`LiveDeskTransport`) and keeps Plaud unconfigured pending
OPS-110. No live transport is ever invoked here -- only the wired object
identities are inspected, and every attempted Plaud call is asserted to
fail closed, in-process, with no I/O of any kind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import plugins.team_duncan_contacts as team_duncan_contacts
from plugins.team_duncan_contacts.collectors.call_history_collector import LiveDeskTransport
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
