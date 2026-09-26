"""Zippy Voice / TUI gateway: server-owned accepted-run lifetime.

Once ``prompt.submit`` is accepted and acknowledged (the ack carries a
``turn_id``), the run belongs to the server — not to the lifetime of the
websocket/request that submitted it. Closing the voice UI, pressing End,
losing the network, or an explicit ``session.close`` must only detach
delivery (drop the transport, stop emitting events) and never reach into
``AIAgent.close()`` / kill inline tools or a launched subprocess. The only
way to actually cancel a running turn is ``session.interrupt`` naming the
exact live ``turn_id``.

See ``.hermes/specs/zippy-end-detach-spec.md`` and
``.hermes/specs/zippy-end-detach-criteria.md`` for the full contract this
file is pinning down, one test per numbered criterion.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from tui_gateway import server


# ── Shared helpers (mirrors the `_session()` builder used throughout
#    tests/test_tui_gateway_server.py and tests/tui_gateway/test_turn_id.py) ──


class _FakeTransport:
    """Minimal write-capable transport — enough for write_json()/_emit()."""

    def write(self, *a, **k):
        return True


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        "close_on_disconnect": False,
        "transport": None,
        **extra,
    }


class _InlineThread:
    """Run a "thread" synchronously — same harness as test_turn_id.py.

    Lets ``_run_prompt_submit``'s conversation run to completion on the
    calling thread so its side effects (history persistence, running flag,
    inflight_turn clear) are observable deterministically, without a real
    OS thread race.
    """

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


@pytest.fixture()
def clean_sessions():
    server._sessions.clear()
    yield
    server._sessions.clear()


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """Neutralize the turn pipeline's environment-heavy side paths.

    Same set as test_turn_id.py's ``turn_env`` — this drives real
    ``_run_prompt_submit`` executions, not a stub.
    """
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


@pytest.fixture()
def no_orphan_timer(monkeypatch):
    """Disable the real grace-window Timer so tests don't leak threads.

    Mirrors _run_disconnect's rationale in tests/test_tui_gateway_ws.py.
    """
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)


@pytest.fixture()
def real_thread_turn_env(monkeypatch, tmp_path):
    """Same neutralizations as ``turn_env``, but WITHOUT mocking
    threading.Thread to run synchronously.

    ``_run_prompt_submit`` starts its conversation on a real background
    thread and returns immediately (mirroring production); the caller must
    join ``session["_run_thread"]``. Needed whenever a test drives genuine
    concurrency against the live turn (e.g. a disconnect racing a blocked
    inline tool) — the synchronous ``_InlineThread`` stand-in used elsewhere
    would have the "disconnect" logic run inline WHILE the real
    ``_run_prompt_submit`` still holds ``_sessions_lock`` around
    ``run_thread.start()``, deadlocking any concurrent ``_sessions_lock``
    user (like ``_close_sessions_for_transport``) started from inside the
    turn itself.
    """
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


# ── Criterion 1: blocked inline tool survives disconnect, run completes ────


def test_blocked_inline_tool_survives_disconnect_and_run_persists(
    real_thread_turn_env, no_orphan_timer
):
    """An inline tool blocked on a clarify-style round trip (``_block``) must
    keep waiting through a disconnect, and once answered (from a second,
    unrelated connection) the turn must finish and persist its result —
    exactly acceptance criterion #1.
    """
    transport = _FakeTransport()
    sid = "sid-blocked-tool"
    observed = {"reaped": None, "detached": None, "running_at_disconnect": None}

    def _disconnect_then_release():
        # Wait for the tool's blocking wait to actually register in _pending
        # (real concurrency: the turn thread races this one).
        deadline = time.time() + 5
        found = None
        while time.time() < deadline and found is None:
            for rid_, (owner_sid, ev) in list(server._pending.items()):
                if owner_sid == sid:
                    found = (rid_, ev)
                    break
            if found is None:
                time.sleep(0.01)
        assert found is not None, "inline tool never registered its blocking wait"

        # The client disconnects WHILE the tool is blocked.
        observed["running_at_disconnect"] = server._sessions[sid].get("running")
        reaped, detached = server._close_sessions_for_transport(
            transport, end_reason="ws_disconnect"
        )
        observed["reaped"], observed["detached"] = reaped, detached

        # A second, later connection answers the still-pending tool prompt.
        rid_, ev = found
        server._answers[rid_] = "go ahead"
        ev.set()

    def _run_conversation(message, stream_callback=None, **kwargs):
        threading.Thread(target=_disconnect_then_release, daemon=True).start()
        answer = server._block("clarify.request", sid, {"question": "continue?"}, timeout=5)
        assert answer == "go ahead"
        return {
            "final_response": "done",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "done"},
            ],
        }

    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=_run_conversation,
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True, transport=transport)
    server._sessions[sid] = session
    try:
        can_start = server._run_prompt_submit("rid", sid, session, "hello")
        assert can_start
        run_thread = session.get("_run_thread")
        assert run_thread is not None
        run_thread.join(timeout=10)
        assert not run_thread.is_alive(), "turn thread never finished"

        # The disconnect only detached delivery — it must not have reaped
        # this (non-close_on_disconnect, running) session.
        assert observed["running_at_disconnect"] is True
        assert observed["reaped"] == 0
        assert observed["detached"] == 1

        # The run actually completed and persisted, despite the disconnect.
        assert session["running"] is False
        assert session["history"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ]
    finally:
        server._sessions.pop(sid, None)


# ── Criterion 2: a launched/background subprocess boundary is not killed ───


def test_disconnect_never_calls_agent_close_while_run_is_accepted(
    clean_sessions, no_orphan_timer
):
    """The ONLY thing that kills a terminal/background-build/claude-build
    launch subprocess is ``AIAgent.close()`` (see run_agent.py). Neither an
    explicit ``session.close`` nor a ``close_on_disconnect`` WS disconnect
    may call it while a turn is accepted and running — acceptance
    criterion #2's "without leaking or killing its subprocess" boundary.
    """
    closed = {"count": 0}
    agent = types.SimpleNamespace(close=lambda: closed.__setitem__("count", closed["count"] + 1))

    # A sidecar/dashboard session (close_on_disconnect=True) with an accepted,
    # still-running turn (e.g. mid claude-build launch tool call).
    transport = _FakeTransport()
    session = _session(agent=agent, running=True, close_on_disconnect=True, transport=transport)
    server._sessions["sid"] = session

    reaped, detached = server._close_sessions_for_transport(transport, end_reason="ws_disconnect")
    assert reaped == 0
    assert detached == 1
    assert closed["count"] == 0
    assert "sid" in server._sessions
    assert server._sessions["sid"]["running"] is True

    # Explicit session.close (the "End" RPC) while still running: also must
    # not close the agent.
    resp = server.handle_request(
        {"id": "1", "method": "session.close", "params": {"session_id": "sid"}}
    )
    assert resp["result"]["closed"] is False
    assert resp["result"]["detached"] is True
    assert closed["count"] == 0
    assert "sid" in server._sessions
    assert server._sessions["sid"]["running"] is True


# ── Criterion 3: reconnect/resume after detached completion, exactly once ──


def test_resume_after_detached_completion_returns_result_exactly_once(clean_sessions, monkeypatch):
    """A session detached mid-turn (disconnect) that then finishes on its own
    must show its result exactly once to a reconnecting session.resume — no
    duplication, no loss.
    """
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    agent = types.SimpleNamespace(session_id="k1")
    session = _session(
        agent=agent,
        running=False,  # the accepted turn already finished
        history=history,
        session_key="k1",
        transport=server._detached_ws_transport,  # still parked from the earlier disconnect
    )
    server._sessions["live-sid"] = session

    class _DB:
        def get_session(self, target):
            return {"id": target, "cwd": "", "message_count": len(history)}

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)

    resp = server.handle_request(
        {"id": "1", "method": "session.resume", "params": {"session_id": "k1"}}
    )
    assert resp.get("result"), f"got error: {resp.get('error')}"
    result = resp["result"]

    # Reused the SAME live session (no duplicate created) and rebound
    # delivery to the reconnecting transport.
    assert result["session_id"] == "live-sid"
    assert server._sessions["live-sid"]["transport"] is not server._detached_ws_transport

    assistant_texts = [m["text"] for m in result["messages"] if m.get("role") == "assistant"]
    assert assistant_texts.count("hi there") == 1

    # A second resume call must not duplicate it either.
    resp2 = server.handle_request(
        {"id": "2", "method": "session.resume", "params": {"session_id": "k1"}}
    )
    assistant_texts_2 = [m["text"] for m in resp2["result"]["messages"] if m.get("role") == "assistant"]
    assert assistant_texts_2.count("hi there") == 1


# ── Criterion 4: disconnect before acceptance creates nothing ──────────────


def test_disconnect_before_prompt_acceptance_creates_no_run(clean_sessions):
    """A prompt.submit racing a disconnect for a session that was never
    accepted (or never existed) must not spin up a run."""
    # (a) prompt.submit against a session id that was never created.
    resp = server.handle_request(
        {
            "id": "1",
            "method": "prompt.submit",
            "params": {"session_id": "never-existed", "text": "hi"},
        }
    )
    assert resp["error"]["code"] == 4001
    assert "never-existed" not in server._sessions

    # (b) a session exists (e.g. session.create happened) but no prompt was
    # ever submitted — its transport disconnecting must simply detach it,
    # never mark it running or spin up a turn thread.
    transport = _FakeTransport()
    session = _session(running=False, transport=transport)
    server._sessions["fresh-sid"] = session

    reaped, detached = server._close_sessions_for_transport(transport, end_reason="ws_disconnect")
    assert reaped == 0
    assert detached == 1
    assert server._sessions["fresh-sid"]["running"] is False
    assert server._sessions["fresh-sid"].get("_run_thread") is None
    assert server._sessions["fresh-sid"]["transport"] is server._detached_ws_transport


# ── Criteria 5 & 6: turn-id-scoped session.interrupt ────────────────────────


def test_interrupt_with_matching_turn_id_cancels_promptly():
    calls = {"interrupted": False}
    agent = types.SimpleNamespace(interrupt=lambda: calls.__setitem__("interrupted", True))
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"turn_id": "turn-current", "assistant": "", "user": "hi"},
    )
    server._sessions["sid"] = session
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "session.interrupt",
                "params": {"session_id": "sid", "turn_id": "turn-current"},
            }
        )
        assert resp.get("result"), f"got error: {resp.get('error')}"
        assert resp["result"]["status"] == "interrupted"
        assert calls["interrupted"] is True
        assert session["_turn_cancel_requested"] is True
    finally:
        server._sessions.pop("sid", None)


def test_interrupt_with_stale_turn_id_cannot_cancel_newer_run():
    calls = {"interrupted": False}
    agent = types.SimpleNamespace(interrupt=lambda: calls.__setitem__("interrupted", True))
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"turn_id": "turn-new", "assistant": "", "user": "hi"},
    )
    server._sessions["sid"] = session
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "session.interrupt",
                "params": {"session_id": "sid", "turn_id": "turn-old-stale"},
            }
        )
        assert resp.get("result"), f"got error: {resp.get('error')}"
        assert resp["result"]["status"] == "stale_turn_id"
        assert resp["result"]["current_turn_id"] == "turn-new"
        # The newer run must be completely untouched.
        assert calls["interrupted"] is False
        assert session["running"] is True
        assert session.get("_turn_cancel_requested") is not True
        assert session["inflight_turn"]["turn_id"] == "turn-new"
    finally:
        server._sessions.pop("sid", None)


def test_interrupt_without_turn_id_keeps_legacy_behavior():
    """Omitting turn_id (older callers) must interrupt whatever is live —
    unchanged from before this feature, per acceptance criterion #10."""
    calls = {"interrupted": False}
    agent = types.SimpleNamespace(interrupt=lambda: calls.__setitem__("interrupted", True))
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"turn_id": "turn-x", "assistant": "", "user": "hi"},
    )
    server._sessions["sid"] = session
    try:
        resp = server.handle_request(
            {"id": "1", "method": "session.interrupt", "params": {"session_id": "sid"}}
        )
        assert resp.get("result"), f"got error: {resp.get('error')}"
        assert resp["result"]["status"] == "interrupted"
        assert calls["interrupted"] is True
    finally:
        server._sessions.pop("sid", None)


# ── Criterion 7: two clients / two sessions, isolated disconnects ──────────


def test_one_disconnect_does_not_cancel_run_or_other_subscriber(clean_sessions, no_orphan_timer):
    transport_a = _FakeTransport()
    transport_b = _FakeTransport()
    agent_a = types.SimpleNamespace(close=lambda: pytest.fail("session A's agent must not be closed"))

    session_a = _session(agent=agent_a, running=True, transport=transport_a)
    session_b = _session(
        agent=types.SimpleNamespace(),
        running=True,
        transport=transport_b,
    )
    server._sessions["sid-a"] = session_a
    server._sessions["sid-b"] = session_b

    reaped, detached = server._close_sessions_for_transport(
        transport_a, end_reason="ws_disconnect"
    )

    assert reaped == 0
    assert detached == 1

    # Session A: detached, but its run keeps running.
    assert server._sessions["sid-a"]["running"] is True
    assert server._sessions["sid-a"]["transport"] is server._detached_ws_transport

    # Session B: completely untouched — different transport, never matched.
    assert server._sessions["sid-b"]["running"] is True
    assert server._sessions["sid-b"]["transport"] is transport_b


# ── Criterion 8: no synthetic completion/replay from detach itself ─────────


def test_detach_paths_emit_no_synthetic_notification(clean_sessions, no_orphan_timer):
    emitted = []
    broadcasts = []

    def _track_emit(*a, **k):
        emitted.append((a, k))

    def _track_broadcast(*a, **k):
        broadcasts.append((a, k))

    session = _session(running=True, close_on_disconnect=True, transport=_FakeTransport())
    server._sessions["sid"] = session
    transport = session["transport"]

    orig_emit = server._emit
    orig_broadcast = server._broadcast_global_event
    server._emit = _track_emit
    server._broadcast_global_event = _track_broadcast
    try:
        server._close_sessions_for_transport(transport, end_reason="ws_disconnect")
        assert emitted == []
        assert broadcasts == []

        # session.close on a second running session must be equally silent.
        # (session.close's handler was rebound onto server's globals at
        # import time, so patching server._emit here reaches it too.)
        session2 = _session(running=True, transport=_FakeTransport())
        server._sessions["sid2"] = session2
        resp = server.handle_request(
            {"id": "1", "method": "session.close", "params": {"session_id": "sid2"}}
        )
        assert resp["result"]["detached"] is True
        assert emitted == []
        assert broadcasts == []
    finally:
        server._emit = orig_emit
        server._broadcast_global_event = orig_broadcast
        server._sessions.pop("sid", None)
        server._sessions.pop("sid2", None)


# ── Criterion 9: the accepted-run registry cleans up, never leaks ──────────


def test_completed_turn_clears_inflight_registry_entry(turn_env):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {"final_response": "ok", "messages": []},
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)
    server._run_prompt_submit("rid", "sid", session, "hi")
    assert session["running"] is False
    assert session["inflight_turn"] is None
    assert server._current_turn_id(session) is None


def test_cancelled_turn_clears_inflight_registry_entry():
    agent = types.SimpleNamespace(interrupt=lambda: None)
    session = _session(
        agent=agent,
        running=True,
        inflight_turn={"turn_id": "t1", "assistant": "", "user": "hi"},
        _run_thread=None,  # no live thread handle -> the safety-net branch fires
    )
    server._sessions["sid"] = session
    try:
        resp = server.handle_request(
            {
                "id": "1",
                "method": "session.interrupt",
                "params": {"session_id": "sid", "turn_id": "t1"},
            }
        )
        assert resp.get("result"), f"got error: {resp.get('error')}"
        assert session["running"] is False
        assert session["inflight_turn"] is None
    finally:
        server._sessions.pop("sid", None)


def test_ws_orphan_reap_does_not_abandon_a_still_running_detached_session(monkeypatch):
    """The grace-window reaper must keep rechecking (not permanently give up
    on) a still-running detached session, and finish the close once the turn
    actually ends — proving no session lingers forever nor gets cancelled.
    """
    timers = []
    torn_down = []

    class _Timer:
        def __init__(self, _delay, fn):
            self.fn = fn
            timers.append(self)

        def start(self):
            return None

    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.01)
    monkeypatch.setattr(server.threading, "Timer", _Timer)
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, *, end_reason="tui_close": torn_down.append(
            (session["_sid"], end_reason)
        ),
    )

    session = _session(running=True, transport=server._detached_ws_transport)
    server._sessions["run-sid"] = session
    try:
        server._schedule_ws_orphan_reap("run-sid")
        assert len(timers) == 1

        # Still running: must reschedule, not tear down.
        timers.pop(0).fn()
        assert torn_down == []
        assert "run-sid" in server._sessions
        assert len(timers) == 1

        # Turn finishes; still detached (no reconnect).
        session["running"] = False
        timers.pop(0).fn()

        assert torn_down == [("run-sid", "ws_orphan_reap")]
        assert "run-sid" not in server._sessions
        assert len(timers) == 0
    finally:
        server._sessions.pop("run-sid", None)
