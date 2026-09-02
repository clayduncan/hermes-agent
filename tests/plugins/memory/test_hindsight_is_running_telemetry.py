"""OPS-14: Tests for the is_running telemetry adapter.

All tests exercise only the adapter; no real daemon is started and no real
network calls are made.
"""
import importlib
import logging
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from plugins.memory.hindsight.is_running_telemetry import install

_LOGGER_NAME = "plugins.memory.hindsight.is_running_telemetry"
_PROFILE = "hermes"
_TEST_URL = "http://127.0.0.1:59999"


def _stub_manager(url=_TEST_URL):
    return SimpleNamespace(get_url=lambda profile: url)


def _mock_client(status_code):
    """Return a mock httpx.Client (context manager) that returns a response."""
    resp = SimpleNamespace(status_code=status_code)
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# Test 1 — 200 path: no failure recorded
# ---------------------------------------------------------------------------

def test_200_returns_true_and_no_failure_log(caplog):
    manager = _stub_manager()
    install(manager)

    with patch("httpx.Client", return_value=_mock_client(200)):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = manager.is_running(_PROFILE)

    assert result is True
    adapter_records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(adapter_records) == 0


# ---------------------------------------------------------------------------
# Test 2 — non-200 path: status and elapsed recorded
# ---------------------------------------------------------------------------

def test_non200_returns_false_and_logs_status(caplog):
    non_200_status = 503
    manager = _stub_manager()
    install(manager)

    with patch("httpx.Client", return_value=_mock_client(non_200_status)):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = manager.is_running(_PROFILE)

    assert result is False

    adapter_records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(adapter_records) == 1

    msg = adapter_records[0].getMessage()
    assert "elapsed_seconds" in msg
    assert "http_status" in msg
    assert str(non_200_status) in msg


# ---------------------------------------------------------------------------
# Test 3 — exception path: class and elapsed recorded
# ---------------------------------------------------------------------------

def test_exception_returns_false_and_logs_class(caplog):
    manager = _stub_manager()
    install(manager)

    exc = httpx.ConnectError("connection refused")
    expected_class = f"{type(exc).__module__}.{type(exc).__qualname__}"

    with patch("httpx.Client", side_effect=exc):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = manager.is_running(_PROFILE)

    assert result is False

    adapter_records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(adapter_records) == 1

    msg = adapter_records[0].getMessage()
    assert "elapsed_seconds" in msg
    assert "exception_class" in msg
    assert expected_class in msg


# ---------------------------------------------------------------------------
# Test 4 — no behavior or pin changes
# ---------------------------------------------------------------------------

def test_no_behavior_or_pin_changes():
    # 1. Adapter does not reference _port_health_ok.
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
