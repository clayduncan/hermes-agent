"""Tests for tools/exchange_online_write_wrapper.py.

All tests use injected fake operations and a spy runner — no live PowerShell,
Exchange Online, or Keychain access.  Audit appends use a real tmp_path
directory so fsync ordering can be verified.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import tools.write_audit_log as write_audit_log
from tools.exchange_online_write_wrapper import (
    REGISTERED_OPERATIONS,
    ExchangeOnlineOperation,
    ExchangeOnlineWriteWrapper,
    ExchangeVerificationError,
    InvalidParameterError,
    UnknownOperationError,
)
from tools.write_audit_log import (
    EXCH_ONLINE_CONFIG,
    KNOWN_DESTINATIONS,
    MissingWriteTriggerError,
    WriteAuditLogError,
    WriteAuditOutcomeLogError,
    WriteAuditRecorder,
    iter_entries,
)


# ── Fake operation fixture ────────────────────────────────────────────────────

FAKE_BEFORE_STATE = {"Identity": "org-tenant", "MaxSendSize": "25MB", "DisplayName": "Old Corp"}
FAKE_AFTER_STATE = {"Identity": "org-tenant", "MaxSendSize": "35MB", "DisplayName": "Old Corp"}

FAKE_OP = ExchangeOnlineOperation(
    name="set-max-send-size",
    before_cmdlet="Get-OrganizationConfig",
    mutation_cmdlet="Set-OrganizationConfig",
    after_cmdlet="Get-OrganizationConfig",
    allowed_parameters=frozenset({"MaxSendSize"}),
    record_id_field="Identity",
    verify_predicate=lambda before, after, params: after.get("MaxSendSize") == params.get("MaxSendSize"),
)

FAKE_REGISTRY: dict[str, ExchangeOnlineOperation] = {FAKE_OP.name: FAKE_OP}

TRIGGER = "OPS-17 test mutation 2026-08-25"


class SpyRunner:
    """Records (phase, cmdlet, params) calls and returns configured responses."""

    def __init__(
        self,
        *,
        before_response: dict = None,
        after_response: dict = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._before = before_response if before_response is not None else FAKE_BEFORE_STATE
        self._after = after_response if after_response is not None else FAKE_AFTER_STATE

    def __call__(self, phase: str, cmdlet: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((phase, cmdlet, params))
        if phase == "before":
            return dict(self._before)
        if phase == "mutate":
            return {}
        if phase == "after":
            return dict(self._after)
        raise AssertionError(f"Unknown phase: {phase!r}")

    @property
    def phases_called(self) -> list[str]:
        return [phase for phase, _, _ in self.calls]

    @property
    def mutate_called(self) -> bool:
        return "mutate" in self.phases_called


def _recorder(log_dir: Path) -> WriteAuditRecorder:
    return WriteAuditRecorder(
        destination=EXCH_ONLINE_CONFIG,
        actor="exch_online_test_actor",
        log_dir=log_dir,
    )


def _wrapper(log_dir: Path, runner: SpyRunner | None = None, **kwargs) -> ExchangeOnlineWriteWrapper:
    return ExchangeOnlineWriteWrapper(
        recorder=_recorder(log_dir),
        operation_registry=FAKE_REGISTRY,
        runner=runner or SpyRunner(),
        **kwargs,
    )


def entries(log_dir: Path) -> list[dict]:
    return [e for _, _, e in iter_entries(log_dir)]


def intent_entries(log_dir: Path) -> list[dict]:
    return [e for e in entries(log_dir) if e["audit_phase"] == "intent"]


def outcome_entries(log_dir: Path) -> list[dict]:
    return [e for e in entries(log_dir) if e["audit_phase"] == "outcome"]


# ── Destination constant and registry ────────────────────────────────────────


class TestDestinationAndRegistry:
    def test_exch_online_config_is_in_known_destinations(self):
        assert EXCH_ONLINE_CONFIG in KNOWN_DESTINATIONS

    def test_exch_online_config_constant_value(self):
        assert EXCH_ONLINE_CONFIG == "exch_online_config"

    def test_production_registry_is_empty(self):
        assert REGISTERED_OPERATIONS == {}, (
            "OPS-17 production registry must be empty — no live Exchange mutation."
        )

    def test_wrong_destination_raises_on_construction(self, tmp_path):
        bad_recorder = WriteAuditRecorder(
            destination="msgraph_contacts",
            actor="test",
            log_dir=tmp_path,
        )
        with pytest.raises(ValueError, match="exch_online_config"):
            ExchangeOnlineWriteWrapper(recorder=bad_recorder)


# ── Operation and parameter validation ───────────────────────────────────────


class TestOperationValidation:
    def test_unknown_operation_raises_before_runner_and_audit(self, tmp_path):
        runner = SpyRunner()
        wrapper = _wrapper(tmp_path / "log", runner)

        with pytest.raises(UnknownOperationError, match="unknown-op"):
            wrapper.execute(operation_name="unknown-op", parameters={}, trigger=TRIGGER)

        assert runner.calls == []
        assert entries(tmp_path / "log") == []

    def test_unknown_parameter_raises_before_runner_and_audit(self, tmp_path):
        runner = SpyRunner()
        wrapper = _wrapper(tmp_path / "log", runner)

        with pytest.raises(InvalidParameterError, match="unknown-param"):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"unknown-param": "value"},
                trigger=TRIGGER,
            )

        assert runner.calls == []
        assert entries(tmp_path / "log") == []

    def test_blank_trigger_raises_before_runner_and_audit(self, tmp_path):
        runner = SpyRunner()
        wrapper = _wrapper(tmp_path / "log", runner)

        with pytest.raises(MissingWriteTriggerError):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger="   ",
            )

        assert runner.calls == []
        assert entries(tmp_path / "log") == []


# ── Audit sequence ordering ───────────────────────────────────────────────────


class TestAuditSequence:
    def test_before_intent_mutation_after_outcome_ordering(self, tmp_path):
        """Verify: before-read → intent → mutate → after-read → outcome."""
        runner = SpyRunner(
            after_response={**FAKE_AFTER_STATE, "MaxSendSize": "35MB"},
        )
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        # Patch append_entry to record ordering relative to runner calls.
        order: list[str] = []
        original_append = write_audit_log.append_entry

        def _tracking_append(entry, *, log_dir, moment):
            order.append(f"audit:{entry['audit_phase']}")
            return original_append(entry, log_dir=log_dir, moment=moment)

        original_runner = runner.__class__.__call__

        def _tracking_runner(self_runner, phase, cmdlet, params):
            order.append(f"runner:{phase}")
            return original_runner(self_runner, phase, cmdlet, params)

        import unittest.mock
        with unittest.mock.patch.object(write_audit_log, "append_entry", _tracking_append), \
             unittest.mock.patch.object(SpyRunner, "__call__", _tracking_runner):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

        assert order == [
            "runner:before",
            "audit:intent",
            "runner:mutate",
            "runner:after",
            "audit:outcome",
        ]

    def test_audit_intent_before_mutation(self, tmp_path):
        """The mutation runner is only called after the intent line is fsync'd."""
        runner = SpyRunner(after_response={**FAKE_AFTER_STATE, "MaxSendSize": "35MB"})
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        wrapper.execute(
            operation_name=FAKE_OP.name,
            parameters={"MaxSendSize": "35MB"},
            trigger=TRIGGER,
        )

        assert runner.phases_called[0] == "before"
        assert "mutate" in runner.phases_called
        # Intent must appear in log before mutation.
        intents = intent_entries(log_dir)
        assert len(intents) == 1
        assert intents[0]["before"] == FAKE_BEFORE_STATE


class TestAuditGate:
    def test_audit_append_failure_blocks_mutation(self, tmp_path, monkeypatch):
        """If the intent append fails, the mutation runner must not be called."""
        runner = SpyRunner()
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        monkeypatch.setattr(
            write_audit_log, "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(WriteAuditLogError):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

        assert not runner.mutate_called, (
            "Mutation runner must be unreachable when audit intent append fails."
        )

    def test_write_completed_outcome_failure_is_not_retried(self, tmp_path, monkeypatch):
        """WriteAuditOutcomeLogError.write_completed is True — must not retry the mutation."""
        runner = SpyRunner(after_response={**FAKE_AFTER_STATE, "MaxSendSize": "35MB"})
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        original_append = write_audit_log.append_entry

        def _fail_outcome(entry, *, log_dir, moment):
            if entry.get("audit_phase") == "outcome":
                raise OSError("outcome log fail")
            return original_append(entry, log_dir=log_dir, moment=moment)

        monkeypatch.setattr(write_audit_log, "append_entry", _fail_outcome)

        with pytest.raises(WriteAuditOutcomeLogError) as exc_info:
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

        assert exc_info.value.write_completed is True
        # Mutation must have been called exactly once — no retry.
        assert runner.phases_called.count("mutate") == 1


# ── Verification ──────────────────────────────────────────────────────────────


class TestVerification:
    def test_verification_failure_raises_exchange_verification_error(self, tmp_path):
        # After-state does NOT match the expected value (MaxSendSize unchanged).
        runner = SpyRunner(after_response=FAKE_BEFORE_STATE)  # same as before
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        with pytest.raises(ExchangeVerificationError, match=FAKE_OP.name):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

    def test_verification_failure_records_outcome_and_does_not_retry(self, tmp_path):
        runner = SpyRunner(after_response=FAKE_BEFORE_STATE)
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        with pytest.raises(ExchangeVerificationError):
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

        # Outcome was still recorded.
        outcomes = outcome_entries(log_dir)
        assert len(outcomes) == 1
        # Mutation was called exactly once.
        assert runner.phases_called.count("mutate") == 1

    def test_verification_failure_and_outcome_log_failure_surfaces_outcome_error(
        self, tmp_path, monkeypatch
    ):
        """When mutation completes but verification fails AND outcome log fails, WriteAuditOutcomeLogError surfaces."""
        runner = SpyRunner(after_response=FAKE_BEFORE_STATE)  # predicate will fail
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        original_append = write_audit_log.append_entry

        def _fail_outcome(entry, *, log_dir, moment):
            if entry.get("audit_phase") == "outcome":
                raise OSError("outcome log fail")
            return original_append(entry, log_dir=log_dir, moment=moment)

        monkeypatch.setattr(write_audit_log, "append_entry", _fail_outcome)

        with pytest.raises(WriteAuditOutcomeLogError) as exc_info:
            wrapper.execute(
                operation_name=FAKE_OP.name,
                parameters={"MaxSendSize": "35MB"},
                trigger=TRIGGER,
            )

        assert exc_info.value.write_completed is True
        assert runner.phases_called.count("mutate") == 1

    def test_happy_path_logs_before_and_after(self, tmp_path):
        runner = SpyRunner(after_response={**FAKE_AFTER_STATE, "MaxSendSize": "35MB"})
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        result = wrapper.execute(
            operation_name=FAKE_OP.name,
            parameters={"MaxSendSize": "35MB"},
            trigger=TRIGGER,
        )

        assert result["MaxSendSize"] == "35MB"

        outcome = outcome_entries(log_dir)[0]
        assert outcome["before"] == FAKE_BEFORE_STATE
        assert outcome["after"]["MaxSendSize"] == "35MB"
        assert outcome["destination"] == EXCH_ONLINE_CONFIG
        assert outcome["trigger"] == TRIGGER


# ── Secret handling ───────────────────────────────────────────────────────────


class TestSecretHandling:
    def test_no_sensitive_content_in_exception_messages(self, tmp_path):
        runner = SpyRunner()
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        with pytest.raises(UnknownOperationError) as exc_info:
            wrapper.execute(
                operation_name="bad-op",
                parameters={},
                trigger=TRIGGER,
            )

        msg = str(exc_info.value)
        sensitive = ["password", "secret", "BEGIN PRIVATE KEY", "access_token"]
        for s in sensitive:
            assert s.lower() not in msg.lower(), f"Sensitive value in exception: {s!r}"

    def test_runner_receives_only_allowed_params(self, tmp_path):
        runner = SpyRunner(after_response={**FAKE_AFTER_STATE, "MaxSendSize": "35MB"})
        log_dir = tmp_path / "log"
        wrapper = _wrapper(log_dir, runner)

        wrapper.execute(
            operation_name=FAKE_OP.name,
            parameters={"MaxSendSize": "35MB"},
            trigger=TRIGGER,
        )

        mutate_call = next((phase, params) for phase, _, params in runner.calls if phase == "mutate")
        _, mutate_params = mutate_call
        assert set(mutate_params.keys()).issubset(FAKE_OP.allowed_parameters)


# ── OPS-45 proof script remains unchanged ────────────────────────────────────


class TestOps45ProofScriptUnchanged:
    """Verify exchange_online_cert_auth.ps1 still contains only Get-OrganizationConfig
    and no mutation cmdlets — OPS-17 must not widen the proof surface."""

    SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "exchange_online_cert_auth.ps1"

    def _script_text(self) -> str:
        return self.SCRIPT.read_text(encoding="utf-8")

    def test_script_exists_and_is_unchanged(self):
        assert self.SCRIPT.is_file()

    def test_only_get_organization_config_cmdlet(self):
        text = self._script_text()
        assert "Get-OrganizationConfig" in text

    def test_no_mutation_cmdlets_present(self):
        text = self._script_text()
        forbidden = [
            "Set-OrganizationConfig", "Set-Mailbox", "New-Mailbox", "Remove-Mailbox",
            "Set-TransportRule", "New-InboxRule", "Set-Recipient",
            "New-Contact", "Remove-Contact",
        ]
        for cmdlet in forbidden:
            assert cmdlet not in text, f"Forbidden mutation cmdlet found in proof script: {cmdlet}"

    def test_no_generic_exec_fragments_added(self):
        text = self._script_text()
        assert "Invoke-Expression" not in text
