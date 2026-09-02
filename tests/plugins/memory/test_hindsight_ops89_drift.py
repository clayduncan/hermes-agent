"""OPS-89 regression: config drift comparison limited to keys in expected_env.

Manager-owned keys in the saved profile env (e.g. HINDSIGHT_API_PORT) do not
count as drift. Missing or changed Hermes-owned keys do count as drift.

All four cases drive the real HindsightMemoryProvider.initialize() and
daemon-start path. Fakes are placed only at external boundaries (the
HindsightEmbedded constructor and the file-system home directory).
"""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _build_embedded_profile_env,
)


_BASE_CONFIG = {
    "mode": "local_embedded",
    "llm_provider": "openai",
    "llm_model": "gpt-4o-mini",
    "llmApiKey": "",
    "profile": "hermes",
}


class _FakeManager:
    def __init__(self, running=True):
        self._running = running
        self.stop_calls = []
        self.started = threading.Event()

    def is_running(self, profile):
        return self._running

    def stop(self, profile):
        self.stop_calls.append(profile)


class _FakeEmbeddedClient:
    def __init__(self, manager):
        self._manager = manager

    def _ensure_started(self):
        self._manager.started.set()


def _env_text(env_dict):
    return "".join(f"{k}={v}\n" for k, v in env_dict.items())


@pytest.fixture()
def _isolated_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def _make_provider(tmp_path, monkeypatch, fake_home, saved_env, running=True):
    """Set up a local_embedded provider with a pre-seeded profile env file.

    Writes the hindsight config and the profile env to isolated paths, patches
    the external boundaries (get_hermes_home, _check_local_runtime,
    _export_port_health_grace_timeout, HindsightEmbedded), then calls the real
    HindsightMemoryProvider.initialize(). Returns (provider, fake_manager).

    The daemon start thread runs inside initialize(). Call
    fake_manager.started.wait(timeout=N) before asserting to ensure the thread
    has run _ensure_started() (or timed out, which would be a test failure).
    """
    hermes_home = tmp_path / "hermes-home"
    config_path = hermes_home / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")

    profile_env_dir = fake_home / ".hindsight" / "profiles"
    profile_env_dir.mkdir(parents=True, exist_ok=True)
    (profile_env_dir / "hermes.env").write_text(_env_text(saved_env), encoding="utf-8")

    monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr("plugins.memory.hindsight._check_local_runtime", lambda: (True, None))
    monkeypatch.setattr(
        "plugins.memory.hindsight._export_port_health_grace_timeout", lambda _cfg: None
    )

    fake_manager = _FakeManager(running=running)

    def _fake_HindsightEmbedded(**kwargs):
        return _FakeEmbeddedClient(fake_manager)

    monkeypatch.setitem(
        sys.modules, "hindsight", SimpleNamespace(HindsightEmbedded=_fake_HindsightEmbedded)
    )

    provider = HindsightMemoryProvider()
    provider.initialize(session_id="test-session")
    return provider, fake_manager


class TestConfigDriftDetection:
    """OPS-89: the drift check compares only keys present in expected_env."""

    def test_saved_env_with_manager_port_no_stop(self, tmp_path, monkeypatch, _isolated_home):
        """Case 1: saved env equals expected Hermes keys plus a manager-owned
        HINDSIGHT_API_PORT key. No drift. The running daemon is not stopped."""
        expected_env = _build_embedded_profile_env(_BASE_CONFIG)
        saved_env = dict(expected_env)
        saved_env["HINDSIGHT_API_PORT"] = "8765"

        _, fake_manager = _make_provider(
            tmp_path, monkeypatch, _isolated_home, saved_env, running=True
        )
        assert fake_manager.started.wait(timeout=5.0), (
            "daemon start thread did not reach _ensure_started within 5s"
        )
        assert fake_manager.stop_calls == [], (
            "manager-owned extra key must not trigger a daemon stop"
        )

    def test_changed_hermes_owned_value_stops_daemon(self, tmp_path, monkeypatch, _isolated_home):
        """Case 2: one Hermes-owned key has a different value in the saved env.
        Drift is detected. The running daemon is stopped before restart."""
        expected_env = _build_embedded_profile_env(_BASE_CONFIG)
        saved_env = dict(expected_env)
        saved_env["HINDSIGHT_API_LLM_MODEL"] = "old-model-name"

        _, fake_manager = _make_provider(
            tmp_path, monkeypatch, _isolated_home, saved_env, running=True
        )
        assert fake_manager.started.wait(timeout=5.0), (
            "daemon start thread did not reach _ensure_started within 5s"
        )
        assert fake_manager.stop_calls, (
            "changed Hermes-owned key must trigger a daemon stop"
        )

    def test_missing_hermes_owned_key_stops_daemon(self, tmp_path, monkeypatch, _isolated_home):
        """Case 3: a Hermes-owned key expected by the config is absent from the
        saved env. Drift is detected. The running daemon is stopped."""
        expected_env = _build_embedded_profile_env(_BASE_CONFIG)
        saved_env = dict(expected_env)
        saved_env.pop("HINDSIGHT_API_LOG_LEVEL", None)

        _, fake_manager = _make_provider(
            tmp_path, monkeypatch, _isolated_home, saved_env, running=True
        )
        assert fake_manager.started.wait(timeout=5.0), (
            "daemon start thread did not reach _ensure_started within 5s"
        )
        assert fake_manager.stop_calls, (
            "missing Hermes-owned key must trigger a daemon stop"
        )

    def test_multiple_manager_owned_keys_no_stop(self, tmp_path, monkeypatch, _isolated_home):
        """Case 4: multiple extra manager-owned keys beyond expected_env. No
        drift. The running daemon is not stopped or restarted."""
        expected_env = _build_embedded_profile_env(_BASE_CONFIG)
        saved_env = dict(expected_env)
        saved_env["HINDSIGHT_API_PORT"] = "8765"
        saved_env["HINDSIGHT_EMBED_DB_PATH"] = "/var/hindsight/db"

        _, fake_manager = _make_provider(
            tmp_path, monkeypatch, _isolated_home, saved_env, running=True
        )
        assert fake_manager.started.wait(timeout=5.0), (
            "daemon start thread did not reach _ensure_started within 5s"
        )
        assert fake_manager.stop_calls == [], (
            "multiple manager-owned extra keys must not trigger a daemon stop"
        )
