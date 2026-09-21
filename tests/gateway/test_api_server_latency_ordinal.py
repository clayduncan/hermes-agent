"""Regression: API-server latency turn_ordinal must survive per-request AIAgent
re-construction.

``gateway/platforms/api_server.py::APIServerAdapter._create_agent`` builds a
brand-new ``AIAgent`` for every HTTP request (see its docstring: "the API
server hands out a fresh UUID session_id per one-off request"). Latency L0's
turn ordinal (agent/latency_metrics.py) used to live ONLY on the agent
instance, so every ``surface=api`` record reported ordinal 1 / band "1-5"
forever, silently invalidating the turns-1-10-vs-15-25 TTFT comparison for
that surface.

The fix threads the existing ``gateway_session_key`` constructor seam (the
same stable per-conversation key ``_create_agent`` already passes through,
and that ``_last_resolved_model`` already keys off) into a bounded,
process-local ordinal registry, so a fresh instance for an already-active
session continues counting.

These tests exercise the REAL ``AIAgent`` construction + ``run_conversation``
path against an in-process mock HTTP provider (no mocked AIAgent), the same
pattern used by tests/agent/test_empty_tool_name_loop_dampening.py.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.latency_metrics import LATENCY_LOGGER_NAME


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(1)
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        resp = {
            "id": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture
def mock_provider():
    _MockHandler.captured_requests = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_latency_ordinal_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]

    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _make_api_agent(base_url: str, *, gateway_session_key):
    """Construct an AIAgent the way APIServerAdapter._create_agent() does for
    one HTTP request: platform="api_server", a fresh instance, carrying the
    caller-supplied stable ``gateway_session_key`` (X-Hermes-Session-Key)."""
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=base_url,
        provider="openai-compat", model="test-model",
        max_iterations=5, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="api_server",
        gateway_session_key=gateway_session_key,
    )
    agent.valid_tool_names = set()
    return agent


def _turn_ordinal_from_latency_log(caplog) -> int:
    records = [r for r in caplog.records if r.name == LATENCY_LOGGER_NAME]
    assert len(records) == 1, f"expected exactly one latency record, got {len(records)}"
    payload = json.loads(records[0].message)
    assert payload["surface"] == "api"
    return payload["turn_ordinal"]


def test_fresh_api_agent_per_request_continues_ordinal_for_same_session(mock_provider, caplog):
    """The core regression: two independently-constructed AIAgent instances
    (as api_server.py builds per HTTP request) sharing the same
    gateway_session_key must report ordinal 1, then 2 — not 1, 1."""
    session_key = "api-abc123def456"

    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER_NAME):
        caplog.clear()
        agent1 = _make_api_agent(mock_provider, gateway_session_key=session_key)
        agent1.run_conversation("first message", conversation_history=[], task_id="t1")
        assert _turn_ordinal_from_latency_log(caplog) == 1

        caplog.clear()
        agent2 = _make_api_agent(mock_provider, gateway_session_key=session_key)
        assert agent2 is not agent1
        agent2.run_conversation("second message", conversation_history=[], task_id="t2")
        assert _turn_ordinal_from_latency_log(caplog) == 2

    # The session key must never appear in any emitted latency log line.
    for r in caplog.records:
        if r.name == LATENCY_LOGGER_NAME:
            assert session_key not in r.message


def test_fresh_api_agent_different_session_key_is_independent(mock_provider, caplog):
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER_NAME):
        caplog.clear()
        agent_a = _make_api_agent(mock_provider, gateway_session_key="api-session-a")
        agent_a.run_conversation("hi", conversation_history=[], task_id="ta")
        assert _turn_ordinal_from_latency_log(caplog) == 1

        caplog.clear()
        agent_b = _make_api_agent(mock_provider, gateway_session_key="api-session-b")
        agent_b.run_conversation("hi", conversation_history=[], task_id="tb")
        assert _turn_ordinal_from_latency_log(caplog) == 1
