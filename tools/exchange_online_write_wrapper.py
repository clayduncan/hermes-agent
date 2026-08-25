"""Audited Exchange Online PowerShell configuration write wrapper.

This module wraps future Exchange Online configuration mutations behind the
same :class:`WriteAuditRecorder` gate used by the Microsoft Graph write
clients.  It is the only supported path for Exchange configuration writes
from this repo.

Key design constraints
======================

- Operation names must come from :data:`REGISTERED_OPERATIONS`.  Callers
  cannot supply arbitrary cmdlets, script text, or shell fragments.
- Each registered operation defines exactly one before-read, one mutation,
  and one after-read PowerShell cmdlet, plus an allow-list of parameter
  names and an exact verification predicate.
- Secrets and certificate material never appear on the command line, in
  stdout/stderr, in the audit log, or in exception messages.
- The audit sequence mirrors the Graph write clients:
  ``before-read → intent-fsync → mutation → after-read → outcome-fsync``.
- The mutation runner is only called after the intent line is fsync'd to
  disk.  An audit-append failure prevents the runner from being invoked.

OPS-17 build state
==================

:data:`REGISTERED_OPERATIONS` is intentionally **empty** in this build.
Tests inject operations and a fake runner; no live PowerShell mutation is
performed.  The production runner exists but is unreachable while the
registry is empty.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.write_audit_log import (
    EXCH_ONLINE_CONFIG,
    WriteAuditOutcomeLogError,
    WriteAuditRecorder,
    require_trigger,
)


# ── Error types ───────────────────────────────────────────────────────────────


class UnknownOperationError(ValueError):
    """Operation name is not in the registered operation registry."""

    def __init__(self, operation_name: str) -> None:
        super().__init__(
            f"Unknown Exchange Online operation {operation_name!r}; "
            "it must be added to REGISTERED_OPERATIONS before use."
        )


class InvalidParameterError(ValueError):
    """One or more parameters are not in the operation's allow-list."""

    def __init__(self, operation_name: str, unknown: set[str]) -> None:
        super().__init__(
            f"Exchange Online operation {operation_name!r} does not allow "
            f"parameter(s): {sorted(unknown)}."
        )


class ExchangeOperationError(RuntimeError):
    """PowerShell runner returned a nonzero exit code or unparseable output."""


class ExchangeVerificationError(RuntimeError):
    """After-state verification predicate returned False after a completed mutation."""


# ── Operation registry type ───────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ExchangeOnlineOperation:
    """Definition of one approved Exchange Online configuration operation.

    Fields
    ------
    name
        Registry key; must match the key in :data:`REGISTERED_OPERATIONS`.
    before_cmdlet
        PowerShell cmdlet used for the before-read step.
    mutation_cmdlet
        PowerShell cmdlet that performs the mutation.
    after_cmdlet
        PowerShell cmdlet used for the after-read verification step.
    allowed_parameters
        Frozenset of caller-supplied parameter names accepted by this
        operation.  Any key outside this set is rejected before the runner
        or audit is invoked.
    record_id_field
        Key in the before/after dict whose value is used as ``record_id``
        in audit log entries.
    verify_predicate
        ``(before, after, params) -> bool``.  Must return ``True`` when the
        after-state reflects the intended change; any ``False`` result is a
        hard failure that stops the run.
    """

    name: str
    before_cmdlet: str
    mutation_cmdlet: str
    after_cmdlet: str
    allowed_parameters: frozenset[str]
    record_id_field: str
    verify_predicate: Callable[[dict[str, Any], dict[str, Any], dict[str, str]], bool]


#: ``runner(phase, cmdlet, params) -> dict`` — injectable for tests.
#: *phase* is ``"before"``, ``"mutate"``, or ``"after"``.
RunnerFn = Callable[[str, str, dict[str, str]], dict[str, Any]]


# ── Production operation registry (empty in OPS-17) ─────────────────────────

#: Registered Exchange Online configuration operations.
#:
#: Empty in OPS-17.  Add entries only when a production mutation is formally
#: scoped, approved, and reviewed.  Tests inject operations via the
#: ``operation_registry`` constructor argument; they do not modify this dict.
REGISTERED_OPERATIONS: dict[str, ExchangeOnlineOperation] = {}


# ── Default production runner (unreachable while registry is empty) ───────────


def _production_runner(
    phase: str,
    cmdlet: str,
    params: dict[str, str],
) -> dict[str, Any]:  # pragma: no cover — unreachable with empty registry
    """Run a registered Exchange Online cmdlet as a PowerShell subprocess.

    Never places secrets on the command line.  The OPS-45 certificate
    connection is established by the called script using macOS Keychain only.

    Raises :class:`ExchangeOperationError` on nonzero exit, unparseable
    output, or empty after-state.
    """
    raise NotImplementedError(
        "Production Exchange Online runner is not active in OPS-17. "
        "Add an operation to REGISTERED_OPERATIONS and provide a real runner."
    )


# ── Wrapper ───────────────────────────────────────────────────────────────────


class ExchangeOnlineWriteWrapper:
    """Gates Exchange Online configuration mutations behind the write-audit log.

    *recorder* must use destination :data:`tools.write_audit_log.EXCH_ONLINE_CONFIG`.

    *operation_registry* defaults to :data:`REGISTERED_OPERATIONS` (empty in
    OPS-17).  Tests inject a dict of fake operations.

    *runner* is the callable that executes PowerShell cmdlets; defaults to
    :func:`_production_runner` (unreachable while the registry is empty).
    Tests inject a fake runner.

    Audit sequence::

        reject unknown operation / unknown parameters
        before-read via runner("before", op.before_cmdlet, params)
        intent fsync (recorder.authorize_write)
        mutation via runner("mutate", op.mutation_cmdlet, params)
        after-read via runner("after", op.after_cmdlet, params)
        verify via op.verify_predicate(before, after, params)
        outcome fsync (authorized.record_outcome)
    """

    def __init__(
        self,
        *,
        recorder: WriteAuditRecorder,
        operation_registry: dict[str, ExchangeOnlineOperation] | None = None,
        runner: RunnerFn | None = None,
    ) -> None:
        if recorder.destination != EXCH_ONLINE_CONFIG:
            raise ValueError(
                f"ExchangeOnlineWriteWrapper requires a recorder with destination "
                f"{EXCH_ONLINE_CONFIG!r}; got {recorder.destination!r}."
            )
        self.recorder = recorder
        self._registry: dict[str, ExchangeOnlineOperation] = (
            operation_registry if operation_registry is not None else REGISTERED_OPERATIONS
        )
        self._runner: RunnerFn = runner if runner is not None else _production_runner

    def execute(
        self,
        *,
        operation_name: str,
        parameters: dict[str, str],
        trigger: str,
    ) -> dict[str, Any]:
        """Execute one registered Exchange Online configuration operation.

        Parameters
        ----------
        operation_name:
            Must be a key in the configured operation registry.
        parameters:
            Caller-supplied values; keys must be a subset of
            ``operation.allowed_parameters``.
        trigger:
            Non-blank human-readable reason for the write.

        Returns the after-state dict returned by the after-read cmdlet.

        Raises
        ------
        UnknownOperationError
            Before any runner call or audit append.
        InvalidParameterError
            Before any runner call or audit append.
        MissingWriteTriggerError
            Before any runner call or audit append.
        WriteAuditLogError
            Audit intent append failed; mutation was NOT attempted.
        WriteAuditOutcomeLogError
            Mutation completed but outcome append failed; do NOT retry.
        ExchangeOperationError
            Runner returned a nonzero exit or unparseable output.
        ExchangeVerificationError
            After-state predicate failed.
        """
        # ── Validate operation and parameters before anything observable. ─────
        if operation_name not in self._registry:
            raise UnknownOperationError(operation_name)
        op = self._registry[operation_name]

        unknown_params = set(parameters) - op.allowed_parameters
        if unknown_params:
            raise InvalidParameterError(operation_name, unknown_params)

        require_trigger(trigger)

        # ── Before-read. ──────────────────────────────────────────────────────
        before_state = self._runner("before", op.before_cmdlet, parameters)
        record_id = before_state.get(op.record_id_field)

        # ── Intent fsync (gate). ──────────────────────────────────────────────
        # Mutation runner is unreachable if this raises WriteAuditLogError.
        authorized = self.recorder.authorize_write(
            operation="update",
            record_id=record_id,
            before=before_state,
            trigger=trigger,
        )

        # ── Mutation. (Only reached if intent line is fsync'd.) ───────────────
        self._runner("mutate", op.mutation_cmdlet, parameters)

        # ── After-read. ───────────────────────────────────────────────────────
        after_state = self._runner("after", op.after_cmdlet, parameters)

        # ── Verification. ─────────────────────────────────────────────────────
        try:
            predicate_passed = op.verify_predicate(before_state, after_state, parameters)
        except Exception as exc:
            authorized.record_outcome(after=after_state)
            raise ExchangeVerificationError(
                f"Verification predicate for {operation_name!r} raised an exception."
            ) from exc

        if not predicate_passed:
            authorized.record_outcome(after=after_state)
            raise ExchangeVerificationError(
                f"After-state verification failed for Exchange operation {operation_name!r}."
            )

        # ── Outcome fsync. ────────────────────────────────────────────────────
        authorized.record_outcome(after=after_state)
        return after_state


__all__ = [
    "EXCH_ONLINE_CONFIG",
    "REGISTERED_OPERATIONS",
    "ExchangeOnlineOperation",
    "ExchangeOnlineWriteWrapper",
    "ExchangeOperationError",
    "ExchangeVerificationError",
    "InvalidParameterError",
    "UnknownOperationError",
    "RunnerFn",
]
