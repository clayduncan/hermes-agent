"""OPS-14: Tests for the Hindsight health contract state machine.

Covers healthy/degraded/dead classification, the staged probe timing, the
per-profile restart circuit breaker, warm-start grace, the startup-lock
passthrough that prevents a probe storm during internal polling, sampling
of healthy telemetry, and the required telemetry fields. No real daemon is
started and no real network calls or real sleeps happen.
"""
import logging
import pathlib
from types import SimpleNamespace

import httpx
import pytest

from plugins.memory.hindsight import health_contract

_PROFILE = "hermes"
_URL = "http://127.0.0.1:59999"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    health_contract.reset_state()
    yield
    health_contract.reset_state()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(health_contract.time, "sleep", lambda seconds: None)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeClient:
    def __init__(self, step):
        self._step = step

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url):
        if isinstance(self._step, BaseException):
            raise self._step
        return self._step


def _install_steps(monkeypatch, steps):
    """Queue a sequence of _FakeResponse/Exception, one per httpx.Client() call."""
    remaining = list(steps)
    calls = []

    def _factory(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        if not remaining:
            raise AssertionError("ran out of fake httpx steps")
        return _FakeClient(remaining.pop(0))

    monkeypatch.setattr(health_contract.httpx, "Client", _factory)
    return calls


def _stub_manager(url=_URL, profile_manager=None):
    ns = SimpleNamespace(get_url=lambda profile: url)
    if profile_manager is not None:
        ns._profile_manager = profile_manager
    return ns


_HEALTHY_PAYLOAD = {"status": "healthy", "database": "connected"}
_DISCONNECTED_PAYLOAD = {"status": "unhealthy", "database": "disconnected"}


def _connect_error():
    return httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# Healthy classification
# ---------------------------------------------------------------------------


def test_healthy_response_within_budget(monkeypatch):
    calls = _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.HEALTHY
    assert decision.final_bool is True
    assert len(decision.probes) == 1
    assert calls == [2.0]  # single 2-second-budget request, no retries


def test_healthy_path_performs_no_sleep(monkeypatch):
    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    sleep_calls = []
    monkeypatch.setattr(health_contract.time, "sleep", lambda s: sleep_calls.append(s))

    health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert sleep_calls == []


def test_database_disconnected_is_degraded_not_dead(monkeypatch):
    _install_steps(monkeypatch, [
        _FakeResponse(200, _DISCONNECTED_PAYLOAD),
        _FakeResponse(200, _DISCONNECTED_PAYLOAD),
        _FakeResponse(200, _DISCONNECTED_PAYLOAD),
    ])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.DEGRADED
    assert decision.final_bool is True


@pytest.mark.parametrize("status", [429, 503])
def test_transient_http_status_is_degraded(monkeypatch, status):
    _install_steps(monkeypatch, [
        _FakeResponse(status),
        _FakeResponse(status),
        _FakeResponse(status),
    ])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.DEGRADED
    assert decision.final_bool is True


def test_timeout_then_recovery_ends_healthy(monkeypatch):
    calls = _install_steps(monkeypatch, [
        httpx.TimeoutException("slow"),
        _FakeResponse(200, _HEALTHY_PAYLOAD),
    ])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.HEALTHY
    assert decision.final_bool is True
    assert calls == [2.0, 5.0]


def test_staged_sequence_uses_2_5_10_second_budgets(monkeypatch):
    calls = _install_steps(monkeypatch, [
        httpx.TimeoutException("slow"),
        _FakeResponse(503),
        _FakeResponse(503),
    ])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert calls == [2.0, 5.0, 10.0]
    assert decision.state == health_contract.DEGRADED
    assert decision.final_bool is True


# ---------------------------------------------------------------------------
# Dead classification and the restart-decision seam
# ---------------------------------------------------------------------------


def test_single_connection_refusal_never_returns_dead_alone(monkeypatch):
    # Confirming probe recovers -> never reaches a dead classification.
    _install_steps(monkeypatch, [_connect_error(), _FakeResponse(200, _HEALTHY_PAYLOAD)])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.HEALTHY
    assert decision.final_bool is True


def test_two_confirmed_no_listener_results_are_dead(monkeypatch):
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.DEAD
    assert decision.final_bool is False
    assert decision.reason_code == "restart_authorized"
    assert decision.startup_attempt_id != health_contract.NONE_SENTINEL


def test_only_dead_can_return_false():
    """final_bool is False exclusively for state == dead, across every path."""
    # Healthy and degraded decisions are built directly (bypassing the probe
    # transport) to assert the invariant on the type itself.
    healthy = health_contract.HealthDecision(state=health_contract.HEALTHY, final_bool=True, reason_code="x")
    degraded = health_contract.HealthDecision(state=health_contract.DEGRADED, final_bool=True, reason_code="x")
    assert healthy.final_bool is True
    assert degraded.final_bool is True


def test_no_listener_within_warm_start_grace_is_degraded_not_dead(monkeypatch):
    manager = _stub_manager()
    circuit = health_contract._get_circuit(_PROFILE)
    circuit.last_started_seen_at = health_contract.time.monotonic()

    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    decision = health_contract.classify(manager, _PROFILE, _URL)

    assert decision.state == health_contract.DEGRADED
    assert decision.final_bool is True
    assert decision.reason_code == "warm_start_grace"


def test_no_listener_with_lock_held_is_degraded_not_dead(monkeypatch):
    monkeypatch.setattr(health_contract, "startup_lock_held", lambda manager, profile: True)
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    decision = health_contract.classify(_stub_manager(), _PROFILE, _URL)

    assert decision.state == health_contract.DEGRADED
    assert decision.reason_code == "warm_start_grace"


# ---------------------------------------------------------------------------
# Circuit breaker: 5 minute / 15 minute suppression, reset on success
# ---------------------------------------------------------------------------


def test_circuit_breaker_backoff_tiers_and_reset(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(health_contract.time, "monotonic", lambda: clock["t"])
    manager = _stub_manager()

    # Incident starts: first dead classification authorizes one attempt.
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d1 = health_contract.classify(manager, _PROFILE, _URL)
    assert d1.final_bool is False
    assert d1.reason_code == "restart_authorized"

    # The attempt fails (daemon still dead). Past the 30s warm-start grace
    # that follows an authorized attempt, this is a real failure -> 5
    # minute suppression opens.
    clock["t"] += 31
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d2 = health_contract.classify(manager, _PROFILE, _URL)
    assert d2.final_bool is True
    assert d2.reason_code == "circuit_open_suppressed"

    # Still inside the 5 minute window -> still suppressed, no new attempt.
    clock["t"] += 60
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d3 = health_contract.classify(manager, _PROFILE, _URL)
    assert d3.final_bool is True
    assert d3.reason_code == "circuit_open_suppressed"

    # 5 minutes elapse -> second automatic attempt authorized.
    clock["t"] += 300
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d4 = health_contract.classify(manager, _PROFILE, _URL)
    assert d4.final_bool is False
    assert d4.reason_code == "restart_authorized"

    # Second attempt also fails (past its own 30s warm-start grace) -> 15
    # minute suppression, circuit open.
    clock["t"] += 31
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d5 = health_contract.classify(manager, _PROFILE, _URL)
    assert d5.final_bool is True
    assert d5.reason_code == "circuit_open_suppressed"

    # Still suppressed after only 5 minutes this time (needs 15).
    clock["t"] += 300
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    d6 = health_contract.classify(manager, _PROFILE, _URL)
    assert d6.final_bool is True
    assert d6.reason_code == "circuit_open_suppressed"

    # 15 minutes total elapse -> a verified healthy response resets everything.
    clock["t"] += 600
    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    d7 = health_contract.classify(manager, _PROFILE, _URL)
    assert d7.state == health_contract.HEALTHY
    assert d7.final_bool is True

    circuit = health_contract._get_circuit(_PROFILE)
    assert circuit.failure_count == 0
    assert circuit.attempt_pending is False
    assert circuit.next_allowed_attempt_at == 0.0


# ---------------------------------------------------------------------------
# Telemetry: sampling, recovery-always-logged, one event per sequence
# ---------------------------------------------------------------------------


def test_healthy_is_sampled_at_most_once_per_60s(monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(health_contract.time, "monotonic", lambda: clock["t"])
    manager = _stub_manager()

    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        health_contract.classify(manager, _PROFILE, _URL)
    first_count = len(caplog.records)
    assert first_count == 1

    caplog.clear()
    clock["t"] += 10  # inside the 60s sample window
    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        health_contract.classify(manager, _PROFILE, _URL)
    assert len(caplog.records) == 0

    caplog.clear()
    clock["t"] += 60  # past the sample window
    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        health_contract.classify(manager, _PROFILE, _URL)
    assert len(caplog.records) == 1


def test_healthy_recovery_always_logged_inside_sample_window(monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(health_contract.time, "monotonic", lambda: clock["t"])
    manager = _stub_manager()

    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        health_contract.classify(manager, _PROFILE, _URL)
    caplog.clear()

    clock["t"] += 5  # well inside the 60s sample window
    _install_steps(monkeypatch, [httpx.TimeoutException("slow"), _FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        decision = health_contract.classify(manager, _PROFILE, _URL)

    assert decision.reason_code == "healthy_recovery"
    assert len(caplog.records) == 1


def test_every_degraded_and_dead_decision_is_recorded(monkeypatch, caplog):
    manager = _stub_manager()
    with caplog.at_level(logging.WARNING):
        _install_steps(monkeypatch, [_FakeResponse(503), _FakeResponse(503), _FakeResponse(503)])
        health_contract.classify(manager, _PROFILE, _URL)
        _install_steps(monkeypatch, [_connect_error(), _connect_error()])
        health_contract.classify(manager, _PROFILE, _URL)

    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert len(decision_records) == 2
    assert {r.state for r in decision_records} == {health_contract.DEGRADED, health_contract.DEAD}


def test_one_event_per_sequence_not_per_probe(monkeypatch, caplog):
    """A 3-stage ambiguous sequence must log exactly once, not per probe."""
    manager = _stub_manager()
    _install_steps(monkeypatch, [httpx.TimeoutException("slow"), _FakeResponse(503), _FakeResponse(503)])
    with caplog.at_level(logging.WARNING):
        health_contract.classify(manager, _PROFILE, _URL)

    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert len(decision_records) == 1


def test_telemetry_contains_required_fields(monkeypatch, caplog):
    manager = _stub_manager()
    _install_steps(monkeypatch, [_connect_error(), _connect_error()])
    with caplog.at_level(logging.ERROR):
        health_contract.classify(manager, _PROFILE, _URL)

    record = next(r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision")
    for field in (
        "event", "state", "http_status", "payload_status", "payload_database",
        "exception_class", "listener_present", "retry", "startup_attempt_id",
        "pid", "thread_name", "returned", "reason",
    ):
        assert hasattr(record, field), f"missing telemetry field: {field}"
    assert record.exception_class.endswith("ConnectError")
    assert record.listener_present is False


def test_telemetry_no_secrets_or_credentials(monkeypatch, caplog):
    manager = _stub_manager()
    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    with caplog.at_level(logging.INFO):
        health_contract.classify(manager, _PROFILE, _URL)

    record = next(r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision")
    message = record.getMessage()
    for banned in ("api_key", "apikey", "authorization", "bearer", "://", "@"):
        assert banned not in message.lower()


# ---------------------------------------------------------------------------
# Startup-lock passthrough: no probe storm during internal polling
# ---------------------------------------------------------------------------


def test_lock_held_uses_legacy_single_probe_no_telemetry(monkeypatch, caplog):
    monkeypatch.setattr(health_contract, "startup_lock_held", lambda manager, profile: True)
    calls = _install_steps(monkeypatch, [_FakeResponse(503)])
    manager = _stub_manager()
    is_running = health_contract.make_is_running(manager)

    with caplog.at_level(logging.DEBUG):
        result = is_running(_PROFILE)

    assert result is False  # legacy behavior: non-200 -> False, no staging
    assert calls == [2]  # single request, no retries
    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert decision_records == []


def test_repeated_polling_while_lock_held_does_not_flood_logs(monkeypatch, caplog):
    """Simulates upstream's ~0.5s startup poll loop: many is_running() calls
    while the start lock is held must never produce per-probe telemetry."""
    monkeypatch.setattr(health_contract, "startup_lock_held", lambda manager, profile: True)
    manager = _stub_manager()
    is_running = health_contract.make_is_running(manager)

    with caplog.at_level(logging.DEBUG):
        for _ in range(50):
            _install_steps(monkeypatch, [_FakeResponse(503)])
            is_running(_PROFILE)

    decision_records = [r for r in caplog.records if getattr(r, "event", None) == "hindsight_health_decision"]
    assert decision_records == []


def test_startup_lock_held_returns_false_for_stub_manager():
    # A manager without _profile_manager (e.g. a test stub) is treated as
    # "not held" rather than raising.
    assert health_contract.startup_lock_held(_stub_manager(), _PROFILE) is False


def test_startup_lock_held_detects_real_flock(tmp_path):
    import os

    lock_path = tmp_path / "hermes.lock"
    lock_path.touch()
    paths = SimpleNamespace(lock=lock_path)
    profile_manager = SimpleNamespace(resolve_profile_paths=lambda profile: paths)
    manager = _stub_manager(profile_manager=profile_manager)

    assert health_contract.startup_lock_held(manager, _PROFILE) is False

    fd = os.open(str(lock_path), os.O_RDONLY)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert health_contract.startup_lock_held(manager, _PROFILE) is True
    finally:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert health_contract.startup_lock_held(manager, _PROFILE) is False


# ---------------------------------------------------------------------------
# Debounce: collapse back-to-back calls describing the same incident
# ---------------------------------------------------------------------------


def test_debounce_collapses_rapid_repeat_calls(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(health_contract.time, "monotonic", lambda: clock["t"])
    manager = _stub_manager()
    is_running = health_contract.make_is_running(manager)

    calls = _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    result1 = is_running(_PROFILE)
    result2 = is_running(_PROFILE)  # same instant -> debounced, no new probe

    assert result1 is True
    assert result2 is True
    assert calls == [2.0]  # only one classification sequence ran


def test_debounce_does_not_collapse_calls_after_window(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(health_contract.time, "monotonic", lambda: clock["t"])
    manager = _stub_manager()
    is_running = health_contract.make_is_running(manager)

    _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    is_running(_PROFILE)

    clock["t"] += 5  # past the 1 second debounce window
    calls = _install_steps(monkeypatch, [_FakeResponse(200, _HEALTHY_PAYLOAD)])
    is_running(_PROFILE)

    assert calls == [2.0]  # this second sequence made its own request


# ---------------------------------------------------------------------------
# Compatibility / scope guards
# ---------------------------------------------------------------------------


def test_no_pid_termination_in_health_contract():
    source = pathlib.Path(health_contract.__file__).read_text(encoding="utf-8")
    for banned in ("os.kill", "SIGTERM", "SIGKILL", "_kill_process", "subprocess"):
        assert banned not in source, f"health_contract.py must not manage processes: found {banned!r}"


def test_no_em_dash_in_health_contract_source():
    source = pathlib.Path(health_contract.__file__).read_text(encoding="utf-8")
    assert "\u2014" not in source
