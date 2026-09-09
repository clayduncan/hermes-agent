"""OPS-14: Tests for the is_running telemetry adapter.

The adapter (plugins/memory/hindsight/is_running_telemetry.py) is a thin
shim that installs health_contract.make_is_running() onto a manager
instance. These tests exercise the installed callable end-to-end through
that shim; the full probe-sequence/circuit-breaker contract is covered in
test_hindsight_health_contract.py. No real daemon is started and no real
network calls are made.
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.hindsight import health_contract
from plugins.memory.hindsight.is_running_telemetry import install

_PROFILE = "hermes"
_TEST_URL = "http://127.0.0.1:59999"


@pytest.fixture(autouse=True)
def _reset_health_contract_state():
    health_contract.reset_state()
    yield
    health_contract.reset_state()


def _stub_manager(url=_TEST_URL):
    return SimpleNamespace(get_url=lambda profile: url)


def _mock_client(status_code, payload=None):
    """Return a mock httpx.Client (context manager) that returns a response."""
    resp = SimpleNamespace(status_code=status_code, json=lambda: payload)
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=resp)
    return client


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(health_contract.time, "sleep", lambda seconds: None)


def test_install_replaces_is_running():
    manager = _stub_manager()
    original = manager.is_running if hasattr(manager, "is_running") else None
    install(manager)
    assert manager.is_running is not original
    assert callable(manager.is_running)


def test_healthy_200_returns_true_no_log(caplog):
    manager = _stub_manager()
    install(manager)

    healthy_payload = {"status": "healthy", "database": "connected"}
    with patch("httpx.Client", return_value=_mock_client(200, healthy_payload)):
        with caplog.at_level(logging.INFO):
            result = manager.is_running(_PROFILE)

    assert result is True
    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    # A first-ever healthy decision is sampled (i.e. logged) since there is
    # no prior sample timestamp for this profile.
    assert len(decision_records) == 1
    assert decision_records[0].state == health_contract.HEALTHY


def test_transient_503_returns_true_degraded_not_dead(caplog):
    """A single non-200 response never authorizes a restart by itself."""
    manager = _stub_manager()
    install(manager)

    with patch("httpx.Client", return_value=_mock_client(503)):
        with caplog.at_level(logging.WARNING):
            result = manager.is_running(_PROFILE)

    assert result is True
    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert len(decision_records) == 1
    assert decision_records[0].state == health_contract.DEGRADED
    assert decision_records[0].returned is True


def test_connection_refused_twice_returns_false_dead(caplog):
    manager = _stub_manager()
    install(manager)

    exc = __import__("httpx").ConnectError("connection refused")
    with patch("httpx.Client", side_effect=exc):
        with caplog.at_level(logging.ERROR):
            result = manager.is_running(_PROFILE)

    assert result is False
    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert len(decision_records) == 1
    assert decision_records[0].state == health_contract.DEAD
    assert decision_records[0].returned is False
    assert decision_records[0].startup_attempt_id != health_contract.NONE_SENTINEL


def test_no_behavior_or_pin_changes():
    import importlib
    import pathlib
    import re

    # 1. Adapter does not reference _port_health_ok (upstream internal we
    #    must not depend on).
    adapter_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "plugins"
        / "memory"
        / "hindsight"
        / "is_running_telemetry.py"
    )
    adapter_src = adapter_path.read_text(encoding="utf-8")
    assert "_port_health_ok" not in adapter_src

    # 2. hindsight-embed version is 0.9.1 in every dependency file.
    root = pathlib.Path(__file__).parent.parent.parent.parent
    dep_files = (
        list(root.glob("requirements*.txt"))
        + list(root.glob("pyproject.toml"))
        + list(root.glob("setup.cfg"))
        + list(root.glob("Pipfile"))
        + list(root.glob("Pipfile.lock"))
    )
    for dep_file in dep_files:
        content = dep_file.read_text(encoding="utf-8")
        versions = re.findall(r"hindsight-embed==([\d.]+)", content)
        for version in versions:
            assert version == "0.9.1", (
                f"Expected hindsight-embed==0.9.1 but found {version!r} in {dep_file}"
            )

    # 3. Config schema key set is unchanged by importing the adapter module.
    hindsight_mod = importlib.import_module("plugins.memory.hindsight")
    provider = hindsight_mod.HindsightMemoryProvider()
    schema_keys_before = {entry["key"] for entry in provider.get_config_schema()}

    importlib.import_module("plugins.memory.hindsight.is_running_telemetry")

    schema_keys_after = {entry["key"] for entry in provider.get_config_schema()}
    assert schema_keys_before == schema_keys_after


def test_no_em_dash_in_adapter_source():
    import pathlib

    adapter_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "plugins"
        / "memory"
        / "hindsight"
        / "is_running_telemetry.py"
    )
    assert "\u2014" not in adapter_path.read_text(encoding="utf-8")
