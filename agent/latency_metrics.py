"""Content-free per-turn latency instrumentation.

Records how long a conversational turn takes to reach first model output,
first audible TTS output, and full completion - without ever touching
prompt/response/tool content, system prompt bytes, memory, URLs,
credentials, or any user/channel/session identifier.

Design:
 - Durations use ``time.monotonic()``. Wall-clock ``time.time()`` is used
   ONLY for the record's ``ts`` field, for cross-record ordering.
 - Every public function on this module is fail-open: it never raises, so
   a bug here can never break a turn (SCOPE C requirement). Failures are
   swallowed and, at worst, produce a record with missing fields.
 - ``surface``/``outcome``/``turn_band`` are drawn from small fixed
   allowlists so no free-form string (a channel id, a chat id, a session
   id, prompt/tool content, ...) can ever land in the emitted record.

One :class:`TurnLatencyRecorder` is created per conversational turn (one
call to ``AIAgent.run_conversation``) and stashed on the agent instance as
``agent._latency_turn``. Call sites in ``run_agent.py`` /
``agent/conversation_loop.py`` / ``gateway/streaming_tts_consumer.py`` call
the small ``note_*`` helper functions below, which look up that recorder
and update it. See AGENTS.md's "Prompt Caching Must Not Break" section -
this module reads agent state defensively (``getattr`` everywhere) and
never mutates provider payloads, messages, or tool schemas.

The turn ordinal (``agent._latency_turn_ordinal``) is normally just an
instance counter - correct as long as one ``AIAgent`` instance lives for
the whole conversation (the native gateway's per-session ``_agent_cache``).
Some surfaces (the API server's ``/v1/*`` handlers) construct a fresh
``AIAgent`` per HTTP request, so an instance-only counter would report
ordinal 1 forever. ``_SessionOrdinalRegistry`` below is a bounded,
thread-safe, process-local map from the existing ``gateway_session_key``
seam (``agent._gateway_session_key`` - the same stable per-conversation key
``_last_resolved_model`` already keys off in ``gateway/platforms/
api_server.py``) to the last-seen ordinal, so a brand-new instance for an
already-active conversation continues counting instead of restarting at 1.
The key is used only as an in-memory dict key - it is never placed in a
latency record, log line, or any other output.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

LATENCY_LOGGER_NAME = "agent.latency"

SCHEMA_VERSION = 1

# Allowlisted turn outcomes - no free-form error strings ever enter the record.
TERMINAL_OUTCOMES = frozenset({
    "success",
    "failed",
    "cancelled",
    "timed_out",
    "no_content",
    "unknown",
    "instrumentation_error",
})

# Allowlisted surfaces (SCOPE A #2: "api/gateway/tui/cli/etc.; no channel/
# user/chat IDs"). Every other ``agent.platform`` value (telegram, discord,
# slack, ...) is a messaging-gateway adapter and buckets to "gateway".
_DIRECT_SURFACES = frozenset({"cli", "tui", "cron", "subagent"})
ALLOWED_SURFACES = frozenset(_DIRECT_SURFACES | {"api", "gateway", "unknown"})

# Bounded turn-index bands (SCOPE B #2 example: "1-5, 6-10, 11-20, 21+").
_BANDS = (
    (1, 5, "1-5"),
    (6, 10, "6-10"),
    (11, 20, "11-20"),
)
_OVERFLOW_BAND = "21+"

logger = logging.getLogger(LATENCY_LOGGER_NAME)


def normalize_surface(platform: Optional[str]) -> str:
    """Map an arbitrary ``agent.platform`` value onto the fixed surface allowlist.

    Never returns anything outside :data:`ALLOWED_SURFACES` - the return
    value is always one of a handful of static strings, regardless of what
    garbage is passed in.
    """
    try:
        value = (platform or "").strip().lower()
    except Exception:
        return "unknown"
    if not value:
        return "unknown"
    if value in _DIRECT_SURFACES:
        return value
    if value == "api_server":
        return "api"
    if value == "desktop":
        return "tui"
    # Any messaging-gateway adapter name (telegram, discord, slack, ...).
    return "gateway"


def turn_band(turn_ordinal: Optional[int]) -> str:
    """Bucket a 1-based turn ordinal into a bounded band label."""
    try:
        n = int(turn_ordinal)
    except (TypeError, ValueError):
        return "unknown"
    if n < 1:
        return "unknown"
    for lo, hi, label in _BANDS:
        if lo <= n <= hi:
            return label
    return _OVERFLOW_BAND


def _isoformat_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _round_ms(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except Exception:
        return None


class TurnLatencyRecorder:
    """Accumulates content-free timing for a single conversational turn.

    Every ``note_*`` method is fail-open - it swallows all exceptions so a
    bug in instrumentation can never interrupt the turn it is observing.
    """

    __slots__ = (
        "surface",
        "turn_ordinal",
        "_turn_start_mono",
        "context_tokens",
        "provider_request_start_mono",
        "first_delta_mono",
        "ttft_ms",
        "model_call_count",
        "total_model_duration_ms",
        "first_pcm_mono",
        "first_pcm_ms",
        "compression_occurred",
        "compression_duration_ms",
        "cache_read_tokens",
        "cache_write_tokens",
        "_cache_seen",
    )

    def __init__(self, *, surface: str, turn_ordinal: int) -> None:
        self.surface = surface if surface in ALLOWED_SURFACES else "unknown"
        self.turn_ordinal = turn_ordinal
        self._turn_start_mono = time.monotonic()
        self.context_tokens: Optional[int] = None
        self.provider_request_start_mono: Optional[float] = None
        self.first_delta_mono: Optional[float] = None
        self.ttft_ms: Optional[float] = None
        self.model_call_count = 0
        self.total_model_duration_ms = 0.0
        self.first_pcm_mono: Optional[float] = None
        self.first_pcm_ms: Optional[float] = None
        self.compression_occurred = False
        self.compression_duration_ms: Optional[float] = None
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self._cache_seen = False

    def note_context_tokens(self, tokens: Any) -> None:
        try:
            self.context_tokens = int(tokens)
        except Exception:
            pass

    def note_provider_call_start(self, started_at_mono: float) -> None:
        try:
            if self.provider_request_start_mono is None:
                self.provider_request_start_mono = float(started_at_mono)
        except Exception:
            pass

    def note_first_delta(self, started_at_mono: float) -> None:
        try:
            if self.first_delta_mono is not None:
                return
            now = time.monotonic()
            self.first_delta_mono = now
            self.ttft_ms = max(0.0, (now - float(started_at_mono)) * 1000.0)
        except Exception:
            pass

    def note_model_call_end(self, started_at_mono: float) -> None:
        try:
            now = time.monotonic()
            self.model_call_count += 1
            self.total_model_duration_ms += max(
                0.0, (now - float(started_at_mono)) * 1000.0
            )
        except Exception:
            pass

    def note_cache_usage(self, read_tokens: Any, write_tokens: Any) -> None:
        try:
            self.cache_read_tokens += int(read_tokens or 0)
            self.cache_write_tokens += int(write_tokens or 0)
            self._cache_seen = True
        except Exception:
            pass

    def note_first_pcm(self) -> None:
        try:
            if self.first_pcm_mono is not None:
                return
            now = time.monotonic()
            self.first_pcm_mono = now
            self.first_pcm_ms = max(0.0, (now - self._turn_start_mono) * 1000.0)
        except Exception:
            pass

    def note_compression(self, occurred: Any, duration_ms: Any = None) -> None:
        try:
            self.compression_occurred = bool(occurred)
            if duration_ms is not None:
                self.compression_duration_ms = float(duration_ms)
        except Exception:
            pass

    def finalize(self, outcome: str) -> Dict[str, Any]:
        """Build the content-free record dict. Never raises."""
        try:
            now = time.monotonic()
            total_ms = max(0.0, (now - self._turn_start_mono) * 1000.0)
            safe_outcome = outcome if outcome in TERMINAL_OUTCOMES else "unknown"
            return {
                "schema_version": SCHEMA_VERSION,
                "ts": _isoformat_now(),
                "surface": self.surface,
                "turn_ordinal": self.turn_ordinal,
                "turn_band": turn_band(self.turn_ordinal),
                "outcome": safe_outcome,
                "context_tokens_estimate": self.context_tokens,
                "model_call_count": self.model_call_count,
                "ttft_ms": _round_ms(self.ttft_ms),
                "total_model_duration_ms": (
                    _round_ms(self.total_model_duration_ms)
                    if self.model_call_count
                    else None
                ),
                "first_pcm_ms": _round_ms(self.first_pcm_ms),
                "compression_occurred": bool(self.compression_occurred),
                "compression_duration_ms": _round_ms(self.compression_duration_ms),
                "cache_read_tokens": self.cache_read_tokens if self._cache_seen else None,
                "cache_write_tokens": self.cache_write_tokens if self._cache_seen else None,
                "total_turn_duration_ms": _round_ms(total_ms),
            }
        except Exception:
            return {
                "schema_version": SCHEMA_VERSION,
                "ts": None,
                "surface": "unknown",
                "turn_ordinal": None,
                "turn_band": "unknown",
                "outcome": "instrumentation_error",
            }


def emit_turn_latency(record: Dict[str, Any]) -> None:
    """Log one content-free latency record as a single JSON line. Never raises."""
    try:
        logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent-facing glue - small, defensive functions that look up the recorder
# stashed on the agent instance. All are no-ops (never raise) when no turn
# is currently being tracked, so every call site can invoke these
# unconditionally with no surrounding try/except.
# ---------------------------------------------------------------------------


_ORDINAL_REGISTRY_MAX_KEYS = 4096
_ORDINAL_REGISTRY_TTL_SECONDS = 6 * 60 * 60  # 6h - bounds memory only; never persisted/logged.


class _SessionOrdinalRegistry:
    """Bounded, thread-safe LRU+TTL map of session-key -> last-seen turn ordinal.

    Lets a fresh ``AIAgent`` instance for an already-active conversation
    (e.g. one created per API request) continue the ordinal sequence a
    prior instance for the SAME conversation left off, without ever
    persisting or emitting the key itself. Process-local only: a process
    restart, idle eviction, or the bound being exceeded resets the counter
    for that conversation, which is an accepted, documented limitation for
    a diagnostic-only metric (see module docstring).
    """

    def __init__(self, max_keys: int = _ORDINAL_REGISTRY_MAX_KEYS,
                 ttl_seconds: float = _ORDINAL_REGISTRY_TTL_SECONDS) -> None:
        self._store: "OrderedDict[str, Tuple[int, float]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._ttl_seconds = ttl_seconds

    def _purge_expired_locked(self, now: float) -> None:
        expired = [k for k, (_, ts) in self._store.items() if now - ts > self._ttl_seconds]
        for k in expired:
            self._store.pop(k, None)

    def bump(self, key: str, known_ordinal: int) -> int:
        """Advance the registry's ordinal for ``key`` past ``known_ordinal``.

        ``known_ordinal`` is whatever this call site already believes the
        last ordinal was (0 if this ``AIAgent`` instance has not tracked a
        turn yet). The registry is authoritative across instances, so the
        new ordinal is ``max(registry_value, known_ordinal) + 1`` - this
        keeps a cache-evicted-and-recreated instance from ever regressing
        or double-counting, and is a pure no-op (matches the pre-existing
        instance-only counter exactly) when only one instance ever touches
        a given key. Never raises.

        The size bound is enforced by evicting the least-recently-bumped
        key(s) AFTER the current key is (re-)inserted as most-recently-used
        - so the key being bumped right now is never the one evicted.
        """
        try:
            now = time.monotonic()
            with self._lock:
                self._purge_expired_locked(now)
                prev, _ts = self._store.pop(key, (0, 0.0))
                ordinal = max(prev, known_ordinal) + 1
                self._store[key] = (ordinal, now)
                while len(self._store) > self._max_keys:
                    self._store.popitem(last=False)
                return ordinal
        except Exception:
            return known_ordinal + 1

    def __len__(self) -> int:
        return len(self._store)


_session_ordinal_registry = _SessionOrdinalRegistry()


def _ordinal_session_key(agent: Any) -> Optional[str]:
    """Best-effort stable per-conversation key for the ordinal registry.

    Returns ``agent._gateway_session_key`` when present - the same seam
    ``api_server.py`` already uses to key its (unbounded, but session-count-
    scale) ``_last_resolved_model`` cache. Deliberately does NOT fall back
    to ``session_id``: the API server mints a fresh UUID ``session_id`` per
    stateless request, and keying on that would grow the registry with a
    one-shot entry per request instead of collapsing onto one entry per
    conversation.
    """
    try:
        key = getattr(agent, "_gateway_session_key", None)
        if isinstance(key, str) and key:
            return key
    except Exception:
        pass
    return None


def start_turn_latency(agent: Any) -> None:
    """Begin tracking a new turn. Called once at the top of ``run_conversation``."""
    try:
        known_ordinal = int(getattr(agent, "_latency_turn_ordinal", 0) or 0)
        session_key = _ordinal_session_key(agent)
        if session_key:
            ordinal = _session_ordinal_registry.bump(session_key, known_ordinal)
        else:
            ordinal = known_ordinal + 1
        agent._latency_turn_ordinal = ordinal
        surface = normalize_surface(getattr(agent, "platform", None))
        agent._latency_turn = TurnLatencyRecorder(surface=surface, turn_ordinal=ordinal)
    except Exception:
        try:
            agent._latency_turn = None
        except Exception:
            pass


def _active_recorder(agent: Any) -> Optional[TurnLatencyRecorder]:
    try:
        return getattr(agent, "_latency_turn", None)
    except Exception:
        return None


def note_context_tokens(agent: Any, tokens: Any) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_context_tokens(tokens)


def note_provider_call_start(agent: Any, started_at_mono: float) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_provider_call_start(started_at_mono)


def note_first_delta(agent: Any, started_at_mono: float) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_first_delta(started_at_mono)


def note_model_call_end(agent: Any, started_at_mono: float) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_model_call_end(started_at_mono)


def note_cache_usage(agent: Any, read_tokens: Any, write_tokens: Any) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_cache_usage(read_tokens, write_tokens)


def note_first_pcm(agent: Any) -> None:
    rec = _active_recorder(agent)
    if rec is not None:
        rec.note_first_pcm()


def finalize_turn_latency(agent: Any, outcome: str) -> Optional[Dict[str, Any]]:
    """End tracking for the current turn, emit the record, and return it.

    Reads the (already-existing, unmodified-by-us) compression telemetry
    signal off the agent - ``_last_compression_attempt_recorded`` is reset
    to ``False`` at the top of every turn by ``conversation_loop.py``, so a
    ``True`` reading here always reflects THIS turn's compression activity.
    """
    rec = None
    try:
        rec = getattr(agent, "_latency_turn", None)
        agent._latency_turn = None
    except Exception:
        pass
    if rec is None:
        return None
    try:
        occurred = bool(getattr(agent, "_last_compression_attempt_recorded", False))
        duration_ms = None
        if occurred:
            compressor = getattr(agent, "context_compressor", None)
            telemetry = getattr(compressor, "_last_compression_telemetry", None)
            if isinstance(telemetry, dict):
                duration_ms = telemetry.get("total_duration_ms")
        rec.note_compression(occurred, duration_ms)
    except Exception:
        pass
    record = rec.finalize(outcome)
    emit_turn_latency(record)
    return record


# ---------------------------------------------------------------------------
# Local aggregation (SCOPE B) - no third-party telemetry, no new database.
# Reads the same rotating ``latency.jsonl`` (+ rotated backups) that
# ``hermes_logging.setup_logging()`` wires up, entirely locally.
# ---------------------------------------------------------------------------

_NUMERIC_METRICS = ("ttft_ms", "first_pcm_ms", "total_model_duration_ms", "total_turn_duration_ms")


def _latency_log_paths(log_dir) -> List[Any]:
    """Return rotated-backup-then-current paths for ``latency.jsonl``, oldest first."""
    base = log_dir / "latency.jsonl"
    paths = []
    # ConcurrentRotatingFileHandler / stdlib RotatingFileHandler both name
    # backups ``<base>.<N>`` with the highest N being the oldest.
    n = 10
    while n >= 1:
        candidate = log_dir / f"latency.jsonl.{n}"
        if candidate.exists():
            paths.append(candidate)
        n -= 1
    if base.exists():
        paths.append(base)
    return paths


def read_latency_records(log_dir) -> Iterator[Dict[str, Any]]:
    """Yield parsed latency records in chronological order. Never raises.

    Malformed lines (partial writes, rotation races) are silently skipped -
    this is a best-effort local diagnostic, not a durable ledger.
    """
    for path in _latency_log_paths(log_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(record, dict):
                        yield record
        except Exception:
            continue


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    """Deterministic nearest-rank percentile. ``sorted_values`` must be sorted."""
    if not sorted_values:
        return None
    n = len(sorted_values)
    import math

    rank = max(1, min(n, math.ceil(pct * n)))
    return sorted_values[rank - 1]


def _stats(values: List[float]) -> Dict[str, Any]:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return {"count": 0, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "count": len(clean),
        "p50": _percentile(clean, 0.50),
        "p95": _percentile(clean, 0.95),
        "min": clean[0],
        "max": clean[-1],
    }


def aggregate_latency(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Group records by (surface, turn_band) and compute count/p50/p95/min/max.

    Returns a dict keyed by ``"<surface>/<turn_band>"`` -> per-metric stats,
    plus a top-level ``context_tokens_by_band`` trend and an
    ``acceptance_comparison`` block (p95 turns 1-10 vs turns 15-25, per the
    Latency L0 acceptance criteria).
    """
    groups: Dict[str, Dict[str, List[float]]] = {}
    band_tokens: Dict[str, List[float]] = {}
    ordinal_ttft: Dict[int, List[float]] = {}

    for record in records:
        surface = record.get("surface")
        surface = surface if surface in ALLOWED_SURFACES else "unknown"
        band = record.get("turn_band") or "unknown"
        key = f"{surface}/{band}"
        bucket = groups.setdefault(key, {m: [] for m in _NUMERIC_METRICS})
        for metric in _NUMERIC_METRICS:
            value = record.get(metric)
            if isinstance(value, (int, float)):
                bucket[metric].append(value)

        tokens = record.get("context_tokens_estimate")
        if isinstance(tokens, (int, float)):
            band_tokens.setdefault(band, []).append(tokens)

        ordinal = record.get("turn_ordinal")
        ttft = record.get("ttft_ms")
        if isinstance(ordinal, int) and isinstance(ttft, (int, float)):
            ordinal_ttft.setdefault(ordinal, []).append(ttft)

    grouped_stats = {
        key: {metric: _stats(values) for metric, values in bucket.items()}
        for key, bucket in groups.items()
    }

    context_tokens_by_band = {
        band: _stats(values) for band, values in band_tokens.items()
    }

    early = [v for ordinal, vs in ordinal_ttft.items() if 1 <= ordinal <= 10 for v in vs]
    late = [v for ordinal, vs in ordinal_ttft.items() if 15 <= ordinal <= 25 for v in vs]

    return {
        "schema_version": SCHEMA_VERSION,
        "groups": grouped_stats,
        "context_tokens_by_band": context_tokens_by_band,
        "acceptance_comparison": {
            "ttft_ms_turns_1_10": _stats(early),
            "ttft_ms_turns_15_25": _stats(late),
        },
    }


def aggregate_from_hermes_home(hermes_home=None) -> Dict[str, Any]:
    """Convenience wrapper: read the live log directory and aggregate it."""
    from hermes_constants import get_hermes_home

    home = hermes_home or get_hermes_home()
    log_dir = home / "logs"
    return aggregate_latency(read_latency_records(log_dir))


def format_report(aggregate: Dict[str, Any]) -> str:
    """Render an :func:`aggregate_latency` result as a plain-text table."""
    lines: List[str] = []
    lines.append("Latency L0 - per-turn timing (content-free)")
    lines.append("")
    lines.append("By surface/turn-band:")
    for key in sorted(aggregate.get("groups", {})):
        bucket = aggregate["groups"][key]
        for metric in _NUMERIC_METRICS:
            s = bucket.get(metric, {})
            if not s.get("count"):
                continue
            lines.append(
                f"  {key:<20} {metric:<24} "
                f"n={s['count']:<5} p50={s['p50']:.1f} p95={s['p95']:.1f} "
                f"min={s['min']:.1f} max={s['max']:.1f}"
            )
    lines.append("")
    lines.append("Context-token estimate by turn band (plateau/trend check):")
    for band in sorted(aggregate.get("context_tokens_by_band", {})):
        s = aggregate["context_tokens_by_band"][band]
        if not s.get("count"):
            continue
        lines.append(
            f"  {band:<10} n={s['count']:<5} p50={s['p50']:.0f} p95={s['p95']:.0f} "
            f"min={s['min']:.0f} max={s['max']:.0f}"
        )
    lines.append("")
    lines.append("Acceptance comparison - TTFT p95, turns 1-10 vs turns 15-25:")
    cmp = aggregate.get("acceptance_comparison", {})
    for label in ("ttft_ms_turns_1_10", "ttft_ms_turns_15_25"):
        s = cmp.get(label, {})
        if not s.get("count"):
            lines.append(f"  {label:<24} n=0 (no data)")
            continue
        lines.append(
            f"  {label:<24} n={s['count']:<5} p50={s['p50']:.1f} p95={s['p95']:.1f}"
        )
    return "\n".join(lines)


__all__ = [
    "LATENCY_LOGGER_NAME",
    "SCHEMA_VERSION",
    "TERMINAL_OUTCOMES",
    "ALLOWED_SURFACES",
    "TurnLatencyRecorder",
    "normalize_surface",
    "turn_band",
    "emit_turn_latency",
    "start_turn_latency",
    "note_context_tokens",
    "note_provider_call_start",
    "note_first_delta",
    "note_model_call_end",
    "note_cache_usage",
    "note_first_pcm",
    "finalize_turn_latency",
    "read_latency_records",
    "aggregate_latency",
    "aggregate_from_hermes_home",
    "format_report",
]
