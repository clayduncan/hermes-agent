"""Tests that the OPS-18 ingestion runner factory wires the Desk production
transport (`LiveDeskTransport`), keeps Plaud unconfigured pending OPS-110,
and -- per Clay's source-isolation correction -- enables only `desk_call`
in the runner's explicit enabled-source contract, so Plaud is never
attempted at all (not merely tolerated on failure). No live transport is
ever invoked here -- only the wired object identities and the
enabled-source configuration are inspected, and every attempted direct
Plaud call is asserted to fail closed, in-process, with no I/O of any kind.

Also covers the OPS-114 regression: standalone automation
(automation_runner.py) never calls register(), so the module-global
`activity_ledger` it used to read directly stayed None there, and every
real runner factory handed its runner an uninitialized ledger. The tests
below call the real factories exactly as standalone automation does --
never register() -- and prove the constructed runner's activity ledger is
non-None and functional.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import plugins.team_duncan_contacts as team_duncan_contacts
from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    DeskCallRecord,
    LiveDeskTransport,
)
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_runner import RunSummary
from plugins.team_duncan_contacts.ingestion_state_db import SOURCE_DESK_CALL, SOURCE_PLAUD
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

CANARY_PHONE = "+15551110001"


@pytest.fixture(autouse=True)
def _reset_module_activity_ledger_global():
    """Force the module-global `activity_ledger` to None for each test in
    this file, matching the exact state standalone automation sees (it
    never calls register()). Importing `.activity_ledger` anywhere in the
    process -- including this file's own import above -- sets that same
    attribute name to the submodule object as a side effect of Python's
    package-import machinery, so without this reset the precondition these
    regression tests depend on would not hold."""
    original = team_duncan_contacts.activity_ledger
    team_duncan_contacts.activity_ledger = None
    yield
    team_duncan_contacts.activity_ledger = original


def _activate(registry: ContactRegistry, phone: str, contact_id: str) -> str:
    reader = FakeGhlReader([{
        "id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Al", "lastName": "Ice", "phone": phone,
    }])
    prep = registry.prepare_activation(contact_id, reader)
    assert prep.status == "ready_for_confirmation", prep.message
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"
    return confirm.contact_id


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


# --- OPS-114 regression: standalone automation must never receive a None
# activity ledger from either real runner factory -------------------------


def test_desk_factory_activity_ledger_is_functional_without_register(tmp_path: Path) -> None:
    """Before the OPS-114 fix, `_build_ingestion_runner_factory`'s runner was
    built with `activity_ledger=activity_ledger` -- the bare module global,
    still None because standalone automation never calls register(). Every
    admit/pre-activation record then raised AttributeError in
    IngestionRunner._process_record.

    Proof: activate a contact with the real registry, build a runner from
    the real factory (never register()), and process one deny_pre_activation
    record straight through the runner's own `_process_record` -- the exact
    method and record shape that raised before the fix. No live Desk fetch,
    no live GHL call: `_process_record` is called directly with a synthetic
    record, and a deny_pre_activation outcome never reaches the note mirror.
    """
    hermes_home = tmp_path / "hermes_home"
    registry = ContactRegistry(
        hermes_home / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID
    )
    _activate(registry, CANARY_PHONE, "c-desk-regression")

    factory = team_duncan_contacts._build_ingestion_runner_factory(hermes_home, registry)
    runner, state_db = factory()

    assert runner._activity_ledger is not None
    assert isinstance(runner._activity_ledger, ActivityLedger)

    before_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    record = DeskCallRecord(
        source=SOURCE_DESK_CALL,
        source_event_id="desk-ops114-regression",
        occurred_at=before_cutoff,
        duration_s=42,
        raw_handle=CANARY_PHONE,
        direction="inbound",
        answered=1,
    )
    summary = RunSummary(run_id=None)

    reached = runner._process_record(SOURCE_DESK_CALL, record, summary)

    assert reached is True
    assert summary.pending_review == 1
    rows = state_db.query_pending_review(source=SOURCE_DESK_CALL)
    assert len(rows) == 1
    assert rows[0].decision == "deny_pre_activation"


def test_plaud_factory_activity_ledger_is_functional_without_register(tmp_path: Path) -> None:
    """Sibling of the Desk regression above: `_build_plaud_summary_runner_factory`
    closed over the same uninitialized module-global `activity_ledger`, so
    the same AttributeError would hit the Plaud automation path the first
    time it recorded an event. So this sibling bug cannot stay masked, this
    test drives one allow-decision event through the exact call
    `PlaudSummaryRunner._process_record` makes -- `_activity_ledger.record_event`
    -- on the real, factory-wired ledger. `_process_record` itself is not
    called here because it requires a sealed Desk re-fetch (a live transport
    call this test must not make); the ledger call it makes is exercised
    directly and deterministically instead.
    """
    hermes_home = tmp_path / "hermes_home"
    registry = ContactRegistry(
        hermes_home / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID
    )
    contact_id = _activate(registry, CANARY_PHONE, "c-plaud-regression")
    ghl_reader = FakeGhlReader([])

    factory = team_duncan_contacts._build_plaud_summary_runner_factory(
        hermes_home, registry, ghl_reader
    )
    runner, _state_db = factory()

    assert runner._activity_ledger is not None
    assert isinstance(runner._activity_ledger, ActivityLedger)

    result = runner._activity_ledger.record_event(
        SOURCE_DESK_CALL,
        "desk-ops114-plaud-regression",
        CANARY_PHONE,
        datetime.now(timezone.utc),
        {"duration_s": 30, "direction": "inbound", "answered": 1},
    )

    assert result.outcome == "admitted"
    events = runner._activity_ledger.query_events(contact_id)
    assert len(events) == 1
