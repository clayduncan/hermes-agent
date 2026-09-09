"""OPS-14: provisional Hindsight embedded-daemon health contract.

Classifies the embedded daemon's ``/health`` probe into one of three states
and owns the single boolean handed back to
``HindsightEmbedded._ensure_started()`` at the restart-decision seam:

  * healthy  -> True.  HTTP 200, ``status == "healthy"``,
    ``database == "connected"``, within the 2 second budget.
  * degraded -> True.  Slow, timed out, a transient non-200 (e.g. 429/503),
    HTTP 200 with an unhealthy payload, or a confirmed no-listener result
    that lands inside an active startup / warm-start grace window. Degraded
    never authorizes a restart; it only means an operation this turn may
    still fail on its own.
  * dead     -> False, but only when no restart-suppressing circuit is
    open. Two confirmed no-listener results, outside any startup activity,
    are required.

A per-profile circuit breaker limits automatic restarts to two attempts per
incident: a failed first attempt suppresses the next one for 5 minutes, and
a failed second attempt latches the circuit open, suppressing every further
automatic attempt regardless of elapsed time. Only a verified healthy
result (or a fresh process, which starts with a clean in-memory circuit)
reopens the path to another automatic attempt. This adapter never kills a
process itself -- the existing upstream exact listener-PID validation
remains the only termination path.

While upstream's own per-profile start lock is held -- the internal startup
poll loop in ``DaemonEmbedManager._start_daemon_locked`` calls
``is_running`` roughly every 0.5 seconds for up to 180 seconds, and another
process can be starting the daemon concurrently -- this adapter falls back
to a single lightweight probe identical to the original upstream
``is_running``. That keeps the multi-stage contract, the circuit breaker,
and telemetry off the hot polling path entirely, which is what prevents a
probe-storm log flood during a slow cold start.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

HEALTHY = "healthy"
DEGRADED = "degraded"
DEAD = "dead"

NONE_SENTINEL = "none"

# Staged probe sequence: timeout budget for each stage, and how long to wait
# before starting that stage (stage 0 runs immediately).
_STAGE_TIMEOUTS = (2.0, 5.0, 10.0)
_STAGE_WAITS = (0.0, 1.0, 2.0)

# A no-listener (connection refused / equivalent) result gets exactly one
# confirming probe after this wait before it can be treated as final.
_NO_LISTENER_CONFIRM_WAIT = 1.0
_NO_LISTENER_CONFIRM_TIMEOUT = 2.0

_WARM_START_GRACE_SECONDS = 30.0
_FIRST_FAILURE_SUPPRESS_SECONDS = 300.0
_SECOND_FAILURE_SUPPRESS_SECONDS = 900.0
_HEALTHY_SAMPLE_INTERVAL_SECONDS = 60.0

# Collapses back-to-back is_running() calls describing the same incident
# (e.g. HindsightEmbedded._ensure_started() and the ensure_running() it
# immediately triggers both probe before any restart can complete) into one
# classification sequence and one telemetry event, and stops the circuit
# breaker from double-counting a single failed attempt.
_DECISION_DEBOUNCE_SECONDS = 1.0

_NO_LISTENER_EXCEPTIONS = (httpx.ConnectError, ConnectionRefusedError)


@dataclass
class _Probe:
    timeout: float
    elapsed: float
    outcome: str  # "healthy" | "ambiguous" | "no_listener"
    http_status: int | None = None
    payload_status: str | None = None
    payload_database: str | None = None
    exception_class: str | None = None


@dataclass
class HealthDecision:
    state: str
    final_bool: bool
    reason_code: str
    probes: list = field(default_factory=list)
    listener_present: bool = False
    startup_attempt_id: str = NONE_SENTINEL


class _ProfileCircuit:
    """Per-profile restart circuit breaker and startup-activity tracking."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failure_count = 0
        self.next_allowed_attempt_at = 0.0
        self.attempt_pending = False
        self.attempt_id = NONE_SENTINEL
        self.last_started_seen_at = 0.0
        self.last_healthy_log_at = 0.0
        # Latched True after the second failed automatic attempt. Unlike
        # next_allowed_attempt_at, this never clears on its own once the
        # suppression window elapses -- only a verified healthy result (or
        # a new process, which starts with a fresh circuit) reopens the
        # path to an automatic restart attempt.
        self.circuit_open = False


_circuits: dict[str, _ProfileCircuit] = {}
_circuits_lock = threading.Lock()

_decision_cache: dict[str, tuple[float, HealthDecision]] = {}
_decision_cache_lock = threading.Lock()


def _get_circuit(profile: str) -> _ProfileCircuit:
    with _circuits_lock:
        circuit = _circuits.get(profile)
        if circuit is None:
            circuit = _ProfileCircuit()
            _circuits[profile] = circuit
        return circuit


def reset_state() -> None:
    """Clear all per-profile circuit and debounce state. Test-only helper."""
    with _circuits_lock:
        _circuits.clear()
    with _decision_cache_lock:
        _decision_cache.clear()


def _fq_exception_class(exc: BaseException) -> str:
    mod = type(exc).__module__
    qualname = type(exc).__qualname__
    return qualname if mod == "builtins" else f"{mod}.{qualname}"


def _classify_response(resp) -> tuple[str, int, str | None, str | None]:
    status = resp.status_code
    if status != 200:
        return "ambiguous", status, None, None
    try:
        payload = resp.json()
    except Exception:
        return "ambiguous", status, None, None
    payload_status = payload.get("status") if isinstance(payload, dict) else None
    payload_database = payload.get("database") if isinstance(payload, dict) else None
    if payload_status == "healthy" and payload_database == "connected":
        return "healthy", status, payload_status, payload_database
    return "ambiguous", status, payload_status, payload_database


def _do_probe(url: str, timeout: float) -> _Probe:
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{url}/health")
        elapsed = time.monotonic() - t0
        outcome, status, payload_status, payload_database = _classify_response(resp)
        return _Probe(
            timeout=timeout, elapsed=elapsed, outcome=outcome, http_status=status,
            payload_status=payload_status, payload_database=payload_database,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        outcome = "no_listener" if isinstance(exc, _NO_LISTENER_EXCEPTIONS) else "ambiguous"
        return _Probe(
            timeout=timeout, elapsed=elapsed, outcome=outcome,
            exception_class=_fq_exception_class(exc),
        )


def _legacy_single_probe(url: str) -> bool:
    """Original upstream ``DaemonEmbedManager.is_running`` behavior.

    Used only while the profile's start lock is held, so the internal
    startup poll loop keeps its original cadence and cost instead of
    running the full multi-stage contract on every 0.5 second tick.
    """
    try:
        with httpx.Client(timeout=2) as client:
            resp = client.get(f"{url}/health")
        return resp.status_code == 200
    except Exception:
        return False


def _resolve_lock_path(manager, profile: str):
    profile_manager = getattr(manager, "_profile_manager", None)
    if profile_manager is None:
        return None
    try:
        paths = profile_manager.resolve_profile_paths(profile)
    except Exception:
        return None
    return getattr(paths, "lock", None)


def startup_lock_held(manager, profile: str) -> bool:
    """Best-effort, read-only probe of upstream's per-profile start lock.

    Never acquires the lock for writing and never blocks -- only checks
    whether some thread or process currently holds it. Missing manager
    internals (e.g. a test stub) are treated as "not held".
    """
    lock_path = _resolve_lock_path(manager, profile)
    if lock_path is None or not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        else:
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
    finally:
        os.close(fd)


def _run_sequence(url: str) -> tuple[list, str, bool]:
    """Run the staged probe sequence.

    Returns (probes, sequence_state, listener_present) where sequence_state
    is one of "healthy", "degraded" (three ambiguous listener-evidence
    outcomes), or "no_listener_confirmed" (two matching no-listener
    results).
    """
    probes: list = []
    stage = 0
    while stage < len(_STAGE_TIMEOUTS):
        wait = _STAGE_WAITS[stage]
        if wait:
            time.sleep(wait)
        probe = _do_probe(url, _STAGE_TIMEOUTS[stage])
        probes.append(probe)

        if probe.outcome == "healthy":
            return probes, HEALTHY, True

        if probe.outcome == "no_listener":
            time.sleep(_NO_LISTENER_CONFIRM_WAIT)
            confirm = _do_probe(url, _NO_LISTENER_CONFIRM_TIMEOUT)
            probes.append(confirm)
            if confirm.outcome == "healthy":
                return probes, HEALTHY, True
            if confirm.outcome == "no_listener":
                return probes, "no_listener_confirmed", False
            # Listener evidence appeared on the confirming probe -- continue
            # the staged sequence instead of declaring dead.
            stage += 1
            continue

        # Ambiguous with listener evidence present.
        stage += 1

    return probes, DEGRADED, True


def classify(manager, profile: str, url: str) -> HealthDecision:
    """Run one full classification sequence and update the profile's circuit."""
    circuit = _get_circuit(profile)
    now = time.monotonic()

    probes, seq_state, listener_present = _run_sequence(url)

    if seq_state == HEALTHY:
        with circuit.lock:
            recovered = circuit.attempt_pending or circuit.failure_count > 0 or circuit.circuit_open
            attempt_id = circuit.attempt_id if circuit.attempt_pending else NONE_SENTINEL
            circuit.attempt_pending = False
            circuit.failure_count = 0
            circuit.next_allowed_attempt_at = 0.0
            circuit.attempt_id = NONE_SENTINEL
            circuit.circuit_open = False
        force = recovered or len(probes) > 1
        reason = "healthy_recovery" if force else "healthy"
        decision = HealthDecision(
            state=HEALTHY, final_bool=True, reason_code=reason, probes=probes,
            listener_present=True, startup_attempt_id=attempt_id,
        )
        _maybe_log_healthy(decision, profile, circuit, force=force)
        return decision

    if seq_state == DEGRADED:
        decision = HealthDecision(
            state=DEGRADED, final_bool=True, reason_code="ambiguous_listener_evidence",
            probes=probes, listener_present=True,
        )
        _log_final(decision, profile, logging.WARNING)
        return decision

    # seq_state == "no_listener_confirmed"
    with circuit.lock:
        within_grace = (now - circuit.last_started_seen_at) < _WARM_START_GRACE_SECONDS
    lock_held = startup_lock_held(manager, profile)
    if lock_held:
        with circuit.lock:
            circuit.last_started_seen_at = now

    if lock_held or within_grace:
        decision = HealthDecision(
            state=DEGRADED, final_bool=True, reason_code="warm_start_grace",
            probes=probes, listener_present=False,
        )
        _log_final(decision, profile, logging.WARNING)
        return decision

    with circuit.lock:
        if circuit.attempt_pending:
            circuit.attempt_pending = False
            circuit.failure_count += 1
            if circuit.failure_count == 1:
                suppress = _FIRST_FAILURE_SUPPRESS_SECONDS
            else:
                suppress = _SECOND_FAILURE_SUPPRESS_SECONDS
                # Latch open. Elapsing the suppression window no longer
                # re-authorizes an automatic attempt on its own -- only a
                # verified healthy result (or a fresh process) resets this.
                circuit.circuit_open = True
            circuit.next_allowed_attempt_at = now + suppress

        if circuit.circuit_open or now < circuit.next_allowed_attempt_at:
            decision = HealthDecision(
                state=DEAD, final_bool=True, reason_code="circuit_open_suppressed",
                probes=probes, listener_present=False,
            )
        else:
            circuit.attempt_pending = True
            circuit.attempt_id = uuid.uuid4().hex
            circuit.last_started_seen_at = now
            decision = HealthDecision(
                state=DEAD, final_bool=False, reason_code="restart_authorized",
                probes=probes, listener_present=False, startup_attempt_id=circuit.attempt_id,
            )
    _log_final(decision, profile, logging.ERROR)
    return decision


def _debounced_classify(manager, profile: str, url: str) -> HealthDecision:
    now = time.monotonic()
    with _decision_cache_lock:
        cached = _decision_cache.get(profile)
        if cached is not None and (now - cached[0]) < _DECISION_DEBOUNCE_SECONDS:
            return cached[1]
    decision = classify(manager, profile, url)
    with _decision_cache_lock:
        _decision_cache[profile] = (time.monotonic(), decision)
    return decision


def _format_probes(probes: list) -> str:
    parts = []
    for p in probes:
        seg = f"{p.timeout:g}s:{p.outcome}:{p.elapsed:.3f}s"
        if p.http_status is not None:
            seg += f":http{p.http_status}"
        if p.exception_class:
            seg += f":{p.exception_class}"
        parts.append(seg)
    return ",".join(parts) if parts else NONE_SENTINEL


def _last(probes: list, attr: str):
    for p in reversed(probes):
        value = getattr(p, attr)
        if value is not None:
            return value
    return None


def _emit(decision: HealthDecision, profile: str, level: int) -> None:
    http_status = _last(decision.probes, "http_status")
    payload_status = _last(decision.probes, "payload_status")
    payload_database = _last(decision.probes, "payload_database")
    exception_class = _last(decision.probes, "exception_class") or NONE_SENTINEL
    retry_total = len(decision.probes)

    fields = {
        "event": "hindsight_health_decision",
        "profile": profile,
        "state": decision.state,
        "http_status": http_status if http_status is not None else NONE_SENTINEL,
        "payload_status": payload_status or NONE_SENTINEL,
        "payload_database": payload_database or NONE_SENTINEL,
        "exception_class": exception_class,
        "listener_present": decision.listener_present,
        "retry": f"{retry_total}/{retry_total}",
        "startup_attempt_id": decision.startup_attempt_id,
        "pid": os.getpid(),
        "thread_name": threading.current_thread().name,
        "returned": decision.final_bool,
        "reason": decision.reason_code,
        "probe_sequence": _format_probes(decision.probes),
    }
    message = "event=hindsight_health_decision " + " ".join(
        f"{key}={value}" for key, value in fields.items() if key != "event"
    )
    logger.log(level, message, extra=fields)


def _log_final(decision: HealthDecision, profile: str, level: int) -> None:
    _emit(decision, profile, level)


def _maybe_log_healthy(decision: HealthDecision, profile: str, circuit: _ProfileCircuit, force: bool) -> None:
    now = time.monotonic()
    with circuit.lock:
        due = force or (now - circuit.last_healthy_log_at) >= _HEALTHY_SAMPLE_INTERVAL_SECONDS
        if due:
            circuit.last_healthy_log_at = now
    if due:
        _emit(decision, profile, logging.INFO)


def make_is_running(manager):
    """Build the ``is_running(profile)`` replacement installed on *manager*."""

    def is_running(profile):
        url = manager.get_url(profile)
        if startup_lock_held(manager, profile):
            return _legacy_single_probe(url)
        decision = _debounced_classify(manager, profile, url)
        return decision.final_bool

    return is_running
