"""OPS-114 no-Zapier architecture: tests for the explicit default-off
``plaud_webhook_enabled`` config gate.

Covers three seams:
  - ``is_plaud_webhook_enabled()`` itself (the config parsing/strictness
    contract), via a monkeypatched ``hermes_cli.config.load_config``.
  - ``plaud_webhook_receiver.main()``, which must fail closed before
    binding a socket, creating/loading the HMAC secret, opening/creating
    the webhook queue, or draining events.
  - ``automation_runner.run(["plaud-webhook", ...])``, which must fail
    closed before lock acquisition, registry/GHL construction, queue
    access, or any state mutation, while leaving ``desk`` and
    ``plaud-reconcile`` unaffected.

No real socket is ever bound, no real config.yaml is ever read or
written, and no real Plaud/GHL/network path is ever reached in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import plugins.team_duncan_contacts as team_duncan_contacts
from plugins.team_duncan_contacts import automation_runner, plaud_webhook_receiver
from plugins.team_duncan_contacts.heartbeat import HeartbeatStore
from plugins.team_duncan_contacts.process_lock import TeamDuncanLock


def _config_with_setting(value):
    if value is _MISSING:
        return {"plugins": {"entries": {"team_duncan_contacts": {"settings": {}}}}}
    return {
        "plugins": {
            "entries": {
                "team_duncan_contacts": {"settings": {"plaud_webhook_enabled": value}}
            }
        }
    }


_MISSING = object()


# ---------------------------------------------------------------------------
# is_plaud_webhook_enabled(): config parsing / strictness contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [_MISSING, None, False, "true", "True", 1, 1.0, [], {}, "yes"],
)
def test_disabled_for_missing_null_false_malformed_and_truthy_non_bool(monkeypatch, value) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config_with_setting(value))
    assert team_duncan_contacts.is_plaud_webhook_enabled() is False


def test_enabled_only_for_literal_boolean_true(monkeypatch) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config_with_setting(True))
    assert team_duncan_contacts.is_plaud_webhook_enabled() is True


def test_disabled_when_config_missing_entirely(monkeypatch) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {})
    assert team_duncan_contacts.is_plaud_webhook_enabled() is False


def test_disabled_when_config_plugins_section_is_not_a_dict(monkeypatch) -> None:
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {"plugins": "not-a-dict"})
    assert team_duncan_contacts.is_plaud_webhook_enabled() is False


def test_disabled_when_load_config_raises(monkeypatch) -> None:
    import hermes_cli.config as config_mod

    def _raise():
        raise RuntimeError("broken config file")

    monkeypatch.setattr(config_mod, "load_config", _raise)
    assert team_duncan_contacts.is_plaud_webhook_enabled() is False


# ---------------------------------------------------------------------------
# plaud_webhook_receiver.main(): fail closed before any side effect.
# ---------------------------------------------------------------------------


def _forbid(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"{name} must not be called while plaud_webhook_enabled is disabled")

    return _raise


def test_main_disabled_never_touches_secret_queue_or_socket(monkeypatch) -> None:
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    monkeypatch.setattr(
        "plugins.team_duncan_contacts.webhook_auth.load_or_create_webhook_secret",
        _forbid("load_or_create_webhook_secret"),
    )
    monkeypatch.setattr(
        plaud_webhook_receiver, "drain_pending_events", _forbid("drain_pending_events")
    )
    monkeypatch.setattr(plaud_webhook_receiver, "build_server", _forbid("build_server"))

    exit_code = plaud_webhook_receiver.main(["--port", "0"])
    assert exit_code == plaud_webhook_receiver.EXIT_DISABLED


def test_main_missing_config_is_disabled(monkeypatch) -> None:
    """Missing config (the real is_plaud_webhook_enabled, backed by a
    monkeypatched load_config that returns no team_duncan_contacts
    settings at all) must fail closed the same way."""
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: {})
    monkeypatch.setattr(
        "plugins.team_duncan_contacts.webhook_auth.load_or_create_webhook_secret",
        _forbid("load_or_create_webhook_secret"),
    )
    monkeypatch.setattr(
        plaud_webhook_receiver, "drain_pending_events", _forbid("drain_pending_events")
    )
    monkeypatch.setattr(plaud_webhook_receiver, "build_server", _forbid("build_server"))

    exit_code = plaud_webhook_receiver.main(["--port", "0"])
    assert exit_code == plaud_webhook_receiver.EXIT_DISABLED


def test_main_enabled_reaches_secret_load_and_build_server(monkeypatch, tmp_path: Path) -> None:
    """Boolean true takes the existing receiver path: secret load, drain,
    and build_server are all reached (build_server itself is stubbed here
    purely to avoid a real serve_forever() call inside this unit test)."""
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    calls: list[str] = []

    def _fake_load_secret(data_dir):
        calls.append("secret")
        return b"\x00" * 32

    def _fake_drain(hermes_home):
        calls.append("drain")
        return []

    class _FakeServer:
        def serve_forever(self):
            calls.append("serve_forever")

        def server_close(self):
            calls.append("server_close")

    def _fake_build_server(host, port, *, secret, hermes_home, path):
        calls.append("build_server")
        return _FakeServer()

    monkeypatch.setattr(
        "plugins.team_duncan_contacts.webhook_auth.load_or_create_webhook_secret",
        _fake_load_secret,
    )
    monkeypatch.setattr(plaud_webhook_receiver, "drain_pending_events", _fake_drain)
    monkeypatch.setattr(plaud_webhook_receiver, "build_server", _fake_build_server)

    exit_code = plaud_webhook_receiver.main(["--port", "0"])
    assert exit_code == 0
    assert calls == ["secret", "drain", "build_server", "serve_forever", "server_close"]


# ---------------------------------------------------------------------------
# automation_runner plaud-webhook mode: fail closed before lock/factory/state.
# ---------------------------------------------------------------------------


def test_plaud_webhook_mode_disabled_returns_exit_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-1"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_DISABLED


def test_plaud_webhook_mode_disabled_never_builds_registry_or_acquires_lock(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader", _forbid("build_registry_and_reader")
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory",
        _forbid("_build_plaud_summary_runner_factory"),
    )

    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-1"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_DISABLED

    # No lock file, no plugin-data directory, no heartbeat -- nothing
    # under hermes_home was ever touched.
    assert not (tmp_path / "plugin-data").exists()
    assert not (tmp_path / "cron").exists()


def test_plaud_webhook_mode_disabled_leaves_lock_free_for_another_process(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    automation_runner.run(["plaud-webhook", "--plaud-recording-id", "rec-1"], hermes_home=tmp_path)

    lock = TeamDuncanLock(hermes_home=tmp_path)
    result = lock.acquire(mode="probe")
    assert result.acquired is True
    lock.release()


def test_plaud_webhook_mode_disabled_output_is_content_free(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-super-secret-id"], hermes_home=tmp_path
    )
    out = capsys.readouterr().out
    assert "rec-super-secret-id" not in out
    import json

    payload = json.loads(out.strip().splitlines()[-1])
    assert payload == {"status": "disabled", "mode": "plaud-webhook"}


def test_desk_and_plaud_reconcile_unaffected_by_disabled_webhook_flag(
    monkeypatch, tmp_path: Path
) -> None:
    """Desk and plaud-reconcile modes must not even consult
    is_plaud_webhook_enabled(); both must still run normally while the
    webhook flag is disabled (the default)."""
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: False)
    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader",
        lambda hermes_home: (object(), object(), "loc-1"),
    )

    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class _FakeSummary:
        counts: dict = field(default_factory=lambda: {"errors": 0, "critical_errors": 0})

        def to_dict(self):
            return dict(self.counts)

    class _FakeRunner:
        def run(self, *, token=None):
            return _FakeSummary()

        def close(self):
            pass

    monkeypatch.setattr(
        team_duncan_contacts, "_build_ingestion_runner_factory",
        lambda hermes_home, registry: (lambda: (_FakeRunner(), object())),
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory",
        lambda hermes_home, registry, ghl_reader: (lambda: (_FakeRunner(), object())),
    )

    desk_exit = automation_runner.run(["desk"], hermes_home=tmp_path)
    reconcile_exit = automation_runner.run(["plaud-reconcile"], hermes_home=tmp_path)

    assert desk_exit == automation_runner.EXIT_COMPLETED
    assert reconcile_exit == automation_runner.EXIT_COMPLETED

    store = HeartbeatStore(tmp_path / "plugin-data" / "team_duncan_contacts")
    assert store.read("desk")["last_outcome"] == "completed"
    assert store.read("plaud_reconcile")["last_outcome"] == "completed"


def test_plaud_webhook_mode_enabled_preserves_existing_path(monkeypatch, tmp_path: Path) -> None:
    """Boolean true does not block the mode -- it reaches process_one() via
    the normal factory/registry wiring, exactly as before this build."""
    monkeypatch.setattr(team_duncan_contacts, "is_plaud_webhook_enabled", lambda: True)
    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader",
        lambda hermes_home: (object(), object(), "loc-1"),
    )

    from dataclasses import dataclass, field

    @dataclass
    class _FakeSummary:
        counts: dict = field(default_factory=lambda: {"errors": 0, "critical_errors": 0})

        def to_dict(self):
            return dict(self.counts)

    calls: list[str] = []

    class _FakeRunner:
        def process_one(self, plaud_recording_id: str):
            calls.append(plaud_recording_id)
            return _FakeSummary()

        def close(self):
            pass

    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory",
        lambda hermes_home, registry, ghl_reader: (lambda: (_FakeRunner(), object())),
    )

    exit_code = automation_runner.run(
        ["plaud-webhook", "--plaud-recording-id", "rec-enabled-1"], hermes_home=tmp_path
    )
    assert exit_code == automation_runner.EXIT_COMPLETED
    assert calls == ["rec-enabled-1"]
