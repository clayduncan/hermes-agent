#!/usr/bin/env python3
"""Sequential model/reasoning latency benchmark for the Zippy voice fast path.

Builds a fresh AIAgent per rep the same way tui_gateway's session.create
would for a ``zippy_voice`` source session (same toolset resolution, same
platform-hint prompt, real config/credentials/memory), sends the fixed
probe prompt, and records content-free timing metrics: no prompt/response
text, no session IDs, no tool arguments or results are written to the
output artifact.

Each rep creates one throwaway session row in the real session store (so
session_search/memory tools see real history) and deletes it again when
the rep finishes - no persistent trace is left behind.

Usage:
    python scripts/voice_fastpath_benchmark.py [--reps N] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPT = "Zippy, do you know what we're testing? Answer briefly."
PLATFORM = "zippy_voice"

# Non-interactive single-turn runs bypass any human approval surface; mirrors
# hermes_cli/oneshot.py's `_run_agent`, which every automated one-shot call
# already relies on. The probe prompt is a read-only question, so no
# destructive tool use is expected.
os.environ.setdefault("HERMES_YOLO_MODE", "1")
os.environ.setdefault("HERMES_ACCEPT_HOOKS", "1")


@dataclass
class RunConfig:
    label: str
    model: str
    provider: str
    reasoning_effort: Optional[str]  # None = provider/config default


CONFIGS: list[RunConfig] = [
    RunConfig("A_gpt-5.6-sol_medium_baseline", "gpt-5.6-sol", "openai-codex", "medium"),
    RunConfig("B_gpt-5.6-sol_low", "gpt-5.6-sol", "openai-codex", "low"),
    RunConfig("B_gpt-5.6-sol_minimal", "gpt-5.6-sol", "openai-codex", "minimal"),
    RunConfig("C_gemini-3-flash-preview_none", "gemini-3-flash-preview", "gemini", "none"),
    RunConfig("D_claude-sonnet-4.6_low", "claude-sonnet-4.6", "anthropic", "low"),
]


@dataclass
class RepResult:
    label: str
    model: str
    provider: str
    reasoning_effort: Optional[str]
    hindsight_healthy: bool
    ok: bool
    error: Optional[str]
    tool_count: int
    tool_names: list[str]
    first_model_action_ms: Optional[float]
    first_content_ms: Optional[float]
    total_duration_ms: float
    correct: Optional[bool] = None  # filled in by manual review pass


def _hindsight_healthy() -> bool:
    try:
        from plugins.memory.hindsight import HindsightMemoryProvider

        return bool(HindsightMemoryProvider().is_available())
    except Exception:
        return False


def _build_enabled_toolsets() -> list[str]:
    """Match what tui_gateway._load_enabled_toolsets() resolves for any
    tui/desktop/zippy_voice session (base resolution keys off the "cli"
    platform_toolsets bucket regardless of the session's own platform tag;
    see tui_gateway/server.py:_load_enabled_toolsets)."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    cfg = load_config()
    return sorted(_get_platform_tools(cfg, "cli", include_default_mcp_servers=True))


def _run_one(cfg: RunConfig, session_db) -> RepResult:
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import parse_reasoning_effort
    from run_agent import AIAgent

    hindsight_ok = _hindsight_healthy()

    tool_events: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def _tool_start(call_id, tool_name, args):  # noqa: ARG001 - args intentionally unused
        tool_events.append({"name": tool_name, "t": time.monotonic() - t0})

    def _clarify(question, choices=None, multi_select=False):  # noqa: ARG001
        return "Use your best judgment; no user is present to answer."

    session_id = f"zippy_voice_bench_{uuid.uuid4().hex[:10]}"
    agent = None
    error: Optional[str] = None
    total_ms = 0.0
    final_response = ""
    try:
        runtime = resolve_runtime_provider(requested=cfg.provider, target_model=cfg.model)
        reasoning_config = (
            parse_reasoning_effort(cfg.reasoning_effort) if cfg.reasoning_effort else None
        )
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            model=cfg.model,
            enabled_toolsets=_build_enabled_toolsets(),
            quiet_mode=True,
            platform=PLATFORM,
            session_id=session_id,
            session_db=session_db,
            credential_pool=runtime.get("credential_pool"),
            reasoning_config=reasoning_config,
            tool_start_callback=_tool_start,
            clarify_callback=_clarify,
        )
        agent.suppress_status_output = True
        agent.stream_delta_callback = None
        agent.tool_gen_callback = None

        t0 = time.monotonic()
        result = agent.run_conversation(PROMPT)
        total_ms = (time.monotonic() - t0) * 1000.0
        final_response = result.get("final_response") or ""
    except Exception as exc:  # noqa: BLE001 - benchmark harness must not crash mid-sweep
        error = f"{type(exc).__name__}: {exc}"
        total_ms = (time.monotonic() - t0) * 1000.0
    finally:
        if agent is not None:
            try:
                agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                agent.close()
            except Exception:
                pass
        try:
            session_db.delete_session(session_id)
        except Exception:
            pass

    first_action_ms = tool_events[0]["t"] * 1000.0 if tool_events else total_ms
    first_content_ms = total_ms if not tool_events else None

    if final_response:
        print(f"    [{cfg.label}] response preview (not saved): {final_response[:160]!r}")

    return RepResult(
        label=cfg.label,
        model=cfg.model,
        provider=cfg.provider,
        reasoning_effort=cfg.reasoning_effort,
        hindsight_healthy=hindsight_ok,
        ok=error is None,
        error=error,
        tool_count=len(tool_events),
        tool_names=[e["name"] for e in tool_events],
        first_model_action_ms=round(first_action_ms, 1),
        first_content_ms=(round(first_content_ms, 1) if first_content_ms is not None else None),
        total_duration_ms=round(total_ms, 1),
    )


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _summarize(label: str, reps: list[RepResult]) -> dict[str, Any]:
    valid = [r for r in reps if r.ok and r.hindsight_healthy]
    totals = [r.total_duration_ms for r in valid]
    actions = [r.first_model_action_ms for r in valid if r.first_model_action_ms is not None]
    return {
        "label": label,
        "reps": len(reps),
        "valid_reps": len(valid),
        "invalid_reason_counts": {
            "error": sum(1 for r in reps if not r.ok),
            "hindsight_unhealthy": sum(1 for r in reps if not r.hindsight_healthy),
        },
        "total_duration_ms": {
            "p50": _percentile(totals, 0.5),
            "p95": _percentile(totals, 0.95),
            "mean": round(statistics.fmean(totals), 1) if totals else None,
        },
        "first_model_action_ms": {
            "p50": _percentile(actions, 0.5),
            "p95": _percentile(actions, 0.95),
            "mean": round(statistics.fmean(actions), 1) if actions else None,
        },
        "avg_tool_count": (
            round(statistics.fmean([r.tool_count for r in valid]), 2) if valid else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=3, help="Reps per config (default 3).")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "evals"
        / "voice_fastpath"
        / "results_latest.json",
    )
    parser.add_argument(
        "--configs",
        type=str,
        default="",
        help="Comma-separated subset of config labels to run (default: all).",
    )
    args = parser.parse_args()

    from hermes_state import SessionDB

    configs = CONFIGS
    if args.configs:
        wanted = {c.strip() for c in args.configs.split(",") if c.strip()}
        configs = [c for c in CONFIGS if c.label in wanted]

    session_db = SessionDB()
    all_reps: list[RepResult] = []
    try:
        for cfg in configs:
            print(f"== {cfg.label} ({cfg.model} / {cfg.provider} / {cfg.reasoning_effort}) ==")
            for i in range(args.reps):
                rep = _run_one(cfg, session_db)
                all_reps.append(rep)
                status = "ok" if rep.ok else f"ERROR: {rep.error}"
                print(
                    f"  rep {i + 1}/{args.reps}: total={rep.total_duration_ms}ms "
                    f"first_action={rep.first_model_action_ms}ms "
                    f"tools={rep.tool_names} hindsight_ok={rep.hindsight_healthy} [{status}]"
                )
    finally:
        session_db.close()

    summary = [_summarize(cfg.label, [r for r in all_reps if r.label == cfg.label]) for cfg in configs]

    artifact = {
        "schema_version": 1,
        "prompt_char_len": len(PROMPT),
        "platform": PLATFORM,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reps": [asdict(r) for r in all_reps],
        "summary": summary,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"\nWrote {args.out}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
