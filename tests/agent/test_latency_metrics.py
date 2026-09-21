"""Tests for the content-free per-turn latency recorder (agent/latency_metrics.py).

Latency L0: these instrumentation records must never carry prompt/response/
tool content, system prompt text, memory, URLs, credentials, or any
user/channel/session identifier - only bounded, allowlisted scalars and
monotonic-derived durations. See AGENTS.md and the Latency L0 task brief.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import latency_metrics as lat


# ---------------------------------------------------------------------------
# Fake monotonic clock - deterministic TTFT / duration math.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A controllable stand-in for time.monotonic()."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(lat.time, "monotonic", clock)
    return clock


# ---------------------------------------------------------------------------
# 1. TTFT success path emits once, with a known fake clock.
# ---------------------------------------------------------------------------


def test_ttft_recorded_once_with_fake_clock(fake_clock):
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    call_start = fake_clock()
    fake_clock.advance(0.25)
    rec.note_first_delta(call_start)
    assert rec.ttft_ms == pytest.approx(250.0)

    # A second delta (e.g. a later tool-call iteration) must not overwrite
    # the first-ever TTFT for this turn.
    fake_clock.advance(1.0)
    rec.note_first_delta(call_start)
    assert rec.ttft_ms == pytest.approx(250.0)


def test_ttft_emits_exactly_one_log_line(fake_clock, caplog):
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    call_start = fake_clock()
    fake_clock.advance(0.1)
    rec.note_first_delta(call_start)
    fake_clock.advance(0.1)
    rec.note_model_call_end(call_start)

    with caplog.at_level(logging.INFO, logger=lat.LATENCY_LOGGER_NAME):
        record = rec.finalize("success")
        lat.emit_turn_latency(record)

    latency_records = [r for r in caplog.records if r.name == lat.LATENCY_LOGGER_NAME]
    assert len(latency_records) == 1
    payload = json.loads(latency_records[0].message)
    assert payload["ttft_ms"] == pytest.approx(100.0)
    assert payload["outcome"] == "success"


# ---------------------------------------------------------------------------
# 2. No-content / failure path.
# ---------------------------------------------------------------------------


def test_no_content_failure_path_emits_with_available_timings(fake_clock):
    rec = lat.TurnLatencyRecorder(surface="gateway", turn_ordinal=3)
    fake_clock.advance(0.05)
    record = rec.finalize("no_content")
    assert record["outcome"] == "no_content"
    assert record["ttft_ms"] is None
    assert record["total_model_duration_ms"] is None
    assert record["total_turn_duration_ms"] == pytest.approx(50.0)


def test_unknown_outcome_string_is_not_passed_through():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    record = rec.finalize("some made-up prompt-derived string")
    assert record["outcome"] == "unknown"
    assert record["outcome"] in lat.TERMINAL_OUTCOMES


def test_finalize_never_raises_even_with_corrupted_state():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    # Simulate an instrumentation bug corrupting internal state badly enough
    # that the normal finalize() arithmetic would raise (non-numeric start
    # time). finalize() must still return a safe, JSON-serializable dict.
    rec._turn_start_mono = "not-a-number"  # type: ignore[assignment]
    record = rec.finalize("success")
    assert record["schema_version"] == lat.SCHEMA_VERSION
    assert record["outcome"] == "instrumentation_error"
    json.dumps(record)  # never breaks the turn - always serializable


# ---------------------------------------------------------------------------
# 3. First PCM emits once; absent-TTS turns produce a safe null.
# ---------------------------------------------------------------------------


def test_first_pcm_recorded_once(fake_clock):
    rec = lat.TurnLatencyRecorder(surface="gateway", turn_ordinal=1)
    fake_clock.advance(0.4)
    rec.note_first_pcm()
    assert rec.first_pcm_ms == pytest.approx(400.0)

    fake_clock.advance(1.0)
    rec.note_first_pcm()
    assert rec.first_pcm_ms == pytest.approx(400.0)  # unchanged


def test_no_tts_produces_null_first_pcm_field():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    record = rec.finalize("success")
    assert record["first_pcm_ms"] is None


# ---------------------------------------------------------------------------
# 4. Token estimate + turn band correctness.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ordinal,expected",
    [
        (0, "unknown"),
        (-1, "unknown"),
        (1, "1-5"),
        (5, "1-5"),
        (6, "6-10"),
        (10, "6-10"),
        (11, "11-20"),
        (20, "11-20"),
        (21, "21+"),
        (999, "21+"),
        (None, "unknown"),
        ("not-a-number", "unknown"),
    ],
)
def test_turn_band_boundaries(ordinal, expected):
    assert lat.turn_band(ordinal) == expected


def test_note_context_tokens_stores_int():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    rec.note_context_tokens(1234.9)
    assert rec.context_tokens == 1234
    record = rec.finalize("success")
    assert record["context_tokens_estimate"] == 1234


def test_note_context_tokens_fails_open_on_bad_input():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=1)
    rec.note_context_tokens(object())  # not int-able
    assert rec.context_tokens is None  # left untouched, no raise


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("cli", "cli"),
        ("tui", "tui"),
        ("cron", "cron"),
        ("subagent", "subagent"),
        ("api_server", "api"),
        ("desktop", "tui"),
        ("telegram", "gateway"),
        ("discord", "gateway"),
        ("whatsapp-business-super-long-adapter-name", "gateway"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_normalize_surface_allowlist(platform, expected):
    result = lat.normalize_surface(platform)
    assert result == expected
    assert result in lat.ALLOWED_SURFACES


# ---------------------------------------------------------------------------
# 5. Aggregation p50/p95 determinism.
# ---------------------------------------------------------------------------


def test_percentile_deterministic_for_known_sample():
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert lat._percentile(sorted(values), 0.50) == 50.0
    assert lat._percentile(sorted(values), 0.95) == 95.0


def test_aggregate_latency_groups_and_computes_stats():
    records = []
    for i in range(1, 11):  # turns 1..10, ttft grows 100..1000ms
        records.append(
            {
                "surface": "cli",
                "turn_band": lat.turn_band(i),
                "turn_ordinal": i,
                "ttft_ms": float(i * 100),
                "context_tokens_estimate": 1000 + i * 10,
            }
        )
    for i in range(15, 26):  # turns 15..25, ttft much higher
        records.append(
            {
                "surface": "cli",
                "turn_band": lat.turn_band(i),
                "turn_ordinal": i,
                "ttft_ms": float(2000 + i * 100),
                "context_tokens_estimate": 5000 + i * 10,
            }
        )

    agg = lat.aggregate_latency(records)
    early = agg["acceptance_comparison"]["ttft_ms_turns_1_10"]
    late = agg["acceptance_comparison"]["ttft_ms_turns_15_25"]
    assert early["count"] == 10
    assert late["count"] == 11
    # The whole point of Latency L0: growth must be visible in this comparison.
    assert late["p95"] > early["p95"]
    assert "cli/1-5" in agg["groups"]
    assert "cli/21+" in agg["groups"]


def test_aggregate_latency_ignores_missing_metrics_safely():
    records = [{"surface": "cli", "turn_band": "1-5", "turn_ordinal": 1}]
    agg = lat.aggregate_latency(records)
    stats = agg["groups"]["cli/1-5"]["ttft_ms"]
    assert stats["count"] == 0
    assert stats["p50"] is None


# ---------------------------------------------------------------------------
# 6. Bounded retention: log wiring uses a fixed backup_count (see
#    tests/test_hermes_logging.py for the handler-attachment test).
# ---------------------------------------------------------------------------


def test_read_latency_records_skips_malformed_lines(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "latency.jsonl").write_text(
        '{"surface": "cli", "turn_ordinal": 1}\n'
        "not json at all\n"
        '{"surface": "tui", "turn_ordinal": 2}\n'
        "\n",
        encoding="utf-8",
    )
    records = list(lat.read_latency_records(log_dir))
    assert len(records) == 2
    assert records[0]["surface"] == "cli"
    assert records[1]["surface"] == "tui"


def test_read_latency_records_reads_rotated_backups_oldest_first(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "latency.jsonl").write_text('{"turn_ordinal": 3}\n', encoding="utf-8")
    (log_dir / "latency.jsonl.1").write_text('{"turn_ordinal": 2}\n', encoding="utf-8")
    (log_dir / "latency.jsonl.2").write_text('{"turn_ordinal": 1}\n', encoding="utf-8")
    records = list(lat.read_latency_records(log_dir))
    assert [r["turn_ordinal"] for r in records] == [1, 2, 3]


def test_read_latency_records_missing_dir_is_safe(tmp_path):
    assert list(lat.read_latency_records(tmp_path / "does-not-exist")) == []


# ---------------------------------------------------------------------------
# 7. No prompt/tool/session/user string can enter the schema or the
#    rendered JSON line - only allowlisted enums, numbers, booleans, None.
# ---------------------------------------------------------------------------


_ALLOWED_STRING_FIELD_VALUES = {
    "surface": lat.ALLOWED_SURFACES,
    "outcome": lat.TERMINAL_OUTCOMES,
}


def test_record_schema_has_no_free_form_strings():
    rec = lat.TurnLatencyRecorder(surface="cli", turn_ordinal=7)
    rec.note_context_tokens(42)
    record = rec.finalize("success")

    for key, value in record.items():
        if not isinstance(value, str):
            continue
        if key in _ALLOWED_STRING_FIELD_VALUES:
            assert value in _ALLOWED_STRING_FIELD_VALUES[key]
        elif key == "turn_band":
            assert value in {"1-5", "6-10", "11-20", "21+", "unknown"}
        elif key == "ts":
            # ISO-8601 wall-clock timestamp, for ordering only - never
            # content, never an identifier.
            assert "T" in value and value.endswith("Z")
        else:
            pytest.fail(f"Unexpected free-form string field {key!r}={value!r}")


def test_surface_injection_attempt_is_neutralized():
    """A platform string crafted to look like a session/user id must never
    survive into the record - normalize_surface collapses it to an enum."""
    hostile_platform = "telegram:chat_id=123456789:user=alice@example.com"
    surface = lat.normalize_surface(hostile_platform)
    assert surface == "gateway"
    assert surface in lat.ALLOWED_SURFACES

    rec = lat.TurnLatencyRecorder(surface=surface, turn_ordinal=1)
    record = rec.finalize("success")
    rendered = json.dumps(record)
    assert "123456789" not in rendered
    assert "alice@example.com" not in rendered


def test_recorder_rejects_disallowed_surface_at_construction():
    rec = lat.TurnLatencyRecorder(surface="not-a-real-surface", turn_ordinal=1)
    assert rec.surface == "unknown"


# ---------------------------------------------------------------------------
# 8. Agent-facing glue is a true no-op when no turn is being tracked, and
#    never mutates the api payload it reads token estimates from (cache/
#    payload byte-equivalence).
# ---------------------------------------------------------------------------


def test_note_functions_are_noop_without_active_recorder():
    agent = SimpleNamespace()  # no _latency_turn attribute at all
    # None of these may raise, and none may create state as a side effect.
    lat.note_context_tokens(agent, 10)
    lat.note_provider_call_start(agent, 1.0)
    lat.note_first_delta(agent, 1.0)
    lat.note_model_call_end(agent, 1.0)
    lat.note_cache_usage(agent, 1, 2)
    lat.note_first_pcm(agent)
    assert lat.finalize_turn_latency(agent, "success") is None


def test_start_and_finalize_turn_latency_roundtrip(fake_clock):
    agent = SimpleNamespace(platform="cli")
    lat.start_turn_latency(agent)
    assert agent._latency_turn_ordinal == 1
    fake_clock.advance(0.3)
    lat.note_context_tokens(agent, 500)
    call_start = fake_clock()
    fake_clock.advance(0.2)
    lat.note_first_delta(agent, call_start)
    lat.note_model_call_end(agent, call_start)

    record = lat.finalize_turn_latency(agent, "success")
    assert record["turn_ordinal"] == 1
    assert record["surface"] == "cli"
    assert record["context_tokens_estimate"] == 500
    assert record["ttft_ms"] == pytest.approx(200.0)
    # The recorder is cleared after finalize - a second call is a no-op.
    assert agent._latency_turn is None
    assert lat.finalize_turn_latency(agent, "success") is None

    # A second turn on the SAME (gateway-cached) agent instance increments
    # the ephemeral, in-process turn ordinal - never a persistent id.
    lat.start_turn_latency(agent)
    assert agent._latency_turn_ordinal == 2


def test_note_context_tokens_does_not_mutate_api_payload():
    """Cache/payload byte-equivalence: reading a token estimate off an API
    payload for instrumentation must never mutate that payload."""
    from agent.chat_completion_helpers import estimate_request_context_tokens

    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello there"}],
        "tools": [{"type": "function", "function": {"name": "noop"}}],
    }
    import copy

    before = copy.deepcopy(payload)
    agent = SimpleNamespace(platform="cli")
    lat.start_turn_latency(agent)
    lat.note_context_tokens(agent, estimate_request_context_tokens(payload))
    assert payload == before


def test_compression_signal_only_reported_when_this_turn_recorded_it():
    compressor = SimpleNamespace(_last_compression_telemetry={"total_duration_ms": 42.0})
    agent = SimpleNamespace(
        platform="cli",
        context_compressor=compressor,
        _last_compression_attempt_recorded=False,
    )
    lat.start_turn_latency(agent)
    record = lat.finalize_turn_latency(agent, "success")
    assert record["compression_occurred"] is False
    assert record["compression_duration_ms"] is None

    agent2 = SimpleNamespace(
        platform="cli",
        context_compressor=compressor,
        _last_compression_attempt_recorded=True,
    )
    lat.start_turn_latency(agent2)
    record2 = lat.finalize_turn_latency(agent2, "success")
    assert record2["compression_occurred"] is True
    assert record2["compression_duration_ms"] == pytest.approx(42.0)
