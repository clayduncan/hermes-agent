"""Per-turn identity on gateway events (voice-turn-id-round19).

The TUI Gateway wire shape used to expose only session identity.
``prompt.submit`` and its ``message.delta`` / ``message.complete`` /
turn-scoped ``error`` / status-progress events shared no explicit turn id, so
a client with two logical generations in flight (Stop local TTS, let Core
keep generating, submit a next prompt) had to guess ownership from FIFO
order — and could misroute new-turn text as stale or suppress new audio.

Contract pinned here:

* ``prompt.submit``'s ack carries a nonempty ``turn_id``.
* Every event attributable to that turn (``message.start``/``delta``/
  ``complete``, turn-scoped ``error``) carries the exact same id, via the
  ambient ``_active_turn_id`` contextvar set once per turn (see
  ``_turn_scope`` in ``tui_gateway/server.py``) rather than threading an
  explicit parameter through every agent callback.
* Two turns (sequential, or a second submitted while the first is queued)
  never share an id and never leak into each other's events.
* A turn interrupted mid-flight still closes with its OWN id on the
  terminal frame.
* Session-level events (no turn active) carry no ``turn_id`` key at all —
  additive, so an old client that doesn't know the field just never sees it.
* The id is transport metadata only: it never reaches the model
  (``conversation_history`` / ``run_message`` / ``persist_user_message``).
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server


class _InlineThread:
    """Run the turn synchronously so tests observe its final state.

    Mirrors the pattern already used by test_prompt_accept_logging.py /
    test_failed_turn_retention.py. Because this does NOT spawn a real OS
    thread, the contextvar _run_prompt_submit sets for the turn is visible
    on the calling (test) thread for the duration of run() — exactly the
    same propagation a real turn thread gets, just without the OS handoff.
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
        **extra,
    }


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """Neutralize the turn pipeline's environment-heavy side paths.

    Deliberately does NOT mock _emit or write_json — these tests exist to
    prove the real _emit()/_event_frame() turn_id plumbing, not to bypass it.
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
def frames(monkeypatch):
    """Capture every raw JSON-RPC frame _emit() would have put on the wire."""
    captured: list = []
    monkeypatch.setattr(server, "write_json", lambda obj: captured.append(obj) or True)
    return captured


def _events(frames, event_type=None, sid=None):
    """List of event ``params`` dicts from captured frames, optionally filtered."""
    out = []
    for f in frames:
        if f.get("method") != "event":
            continue
        params = f.get("params") or {}
        if event_type is not None and params.get("type") != event_type:
            continue
        if sid is not None and params.get("session_id") != sid:
            continue
        out.append(params)
    return out


# ── Single turn: ack + every event share one id ────────────────────────


def test_turn_events_all_share_one_nonempty_id(turn_env, frames):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda message, stream_callback=None, **kwargs: (
            stream_callback("partial ") or stream_callback("reply")
        )
        or {"final_response": "partial reply"},
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "hello")

    starts = _events(frames, "message.start", sid="sid")
    deltas = _events(frames, "message.delta", sid="sid")
    completes = _events(frames, "message.complete", sid="sid")
    assert len(starts) == 1
    assert len(deltas) == 2
    assert len(completes) == 1

    turn_id = starts[0].get("turn_id")
    assert turn_id, "message.start must carry a nonempty turn_id"
    assert all(d.get("turn_id") == turn_id for d in deltas)
    assert completes[0].get("turn_id") == turn_id


def test_prompt_submit_ack_turn_id_matches_first_event(turn_env, frames, monkeypatch):
    """The id returned in the ack is the SAME id every event for that turn
    carries — not a distinct value minted independently."""
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda session, rid, sid: None)

    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {"final_response": "done"},
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=False)
    server._sessions["sid"] = session
    try:
        resp = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "hello"}
        )
    finally:
        server._sessions.pop("sid", None)

    assert resp.get("result"), f"got error: {resp.get('error')}"
    ack_turn_id = resp["result"]["turn_id"]
    assert ack_turn_id

    starts = _events(frames, "message.start", sid="sid")
    completes = _events(frames, "message.complete", sid="sid")
    assert starts and starts[0]["turn_id"] == ack_turn_id
    assert completes and completes[0]["turn_id"] == ack_turn_id


# ── Two turns: distinct ids, no crossover ──────────────────────────────


def test_two_sequential_turns_get_distinct_ids(turn_env, frames):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {"final_response": "ok"},
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid-1", "sid", session, "first")
    session["running"] = True
    server._run_prompt_submit("rid-2", "sid", session, "second")

    completes = _events(frames, "message.complete", sid="sid")
    assert len(completes) == 2
    id_1, id_2 = completes[0]["turn_id"], completes[1]["turn_id"]
    assert id_1 and id_2
    assert id_1 != id_2

    # Every frame belongs to exactly one of the two turns — no event carries
    # a foreign id and none is left blank.
    all_turn_ids = {
        f["params"]["turn_id"]
        for f in frames
        if f.get("method") == "event" and f["params"].get("turn_id")
    }
    assert all_turn_ids == {id_1, id_2}


def test_queued_second_turn_does_not_reuse_first_turns_id(turn_env, frames):
    """A prompt that arrives while the first is still running (drained via
    _drain_queued_prompt once the first ends) gets its own fresh id."""
    calls = {"n": 0}

    def _run(message, stream_callback=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Queue a second prompt before this (first) turn concludes.
            server._enqueue_prompt(session, "second", None)
        return {"final_response": f"reply {calls['n']}"}

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid-1", "sid", session, "first")

    completes = _events(frames, "message.complete", sid="sid")
    assert len(completes) == 2
    assert completes[0]["turn_id"] != completes[1]["turn_id"]
    assert calls["n"] == 2


# ── Interrupt / error paths preserve the interrupted turn's id ─────────


def test_interrupted_turn_terminal_frame_preserves_its_id(turn_env, frames):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {
            "final_response": "",
            "interrupted": True,
        },
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "stop me")

    starts = _events(frames, "message.start", sid="sid")
    completes = _events(frames, "message.complete", sid="sid")
    assert completes[0]["payload"]["status"] == "interrupted"
    assert completes[0]["turn_id"] == starts[0]["turn_id"]


def test_returned_error_terminal_frame_preserves_turn_id(turn_env, frames):
    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {
            "final_response": "",
            "error": "provider 402: billing wall",
            "failed": True,
        },
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "do the thing")

    starts = _events(frames, "message.start", sid="sid")
    completes = _events(frames, "message.complete", sid="sid")
    assert completes[0]["payload"]["status"] == "error"
    assert completes[0]["turn_id"] == starts[0]["turn_id"]

    # Resume must be able to recover the same id for this retained failure.
    snapshot = server._inflight_snapshot(session)
    assert snapshot["turn_id"] == starts[0]["turn_id"]


def test_exception_path_terminal_frame_preserves_turn_id(turn_env, frames):
    def _boom(message, stream_callback=None, **kwargs):
        if stream_callback is not None:
            stream_callback("half an ans")
        raise RuntimeError("connection reset mid-stream")

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_boom, clear_interrupt=lambda: None
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "do the thing")

    starts = _events(frames, "message.start", sid="sid")
    completes = _events(frames, "message.complete", sid="sid")
    assert completes[0]["payload"]["status"] == "error"
    assert completes[0]["turn_id"] == starts[0]["turn_id"]


def test_cancelled_before_agent_ready_error_event_carries_turn_id(monkeypatch):
    """A turn cancelled while the agent is still (deferred) building must
    surface an error event carrying the SAME id the ack promised — not a
    bare id-less error (issue #63078 server-side half, extended for turn id).
    """
    threads = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            threads.append(self)

        def start(self):
            return None

        def is_alive(self):
            return True

    captured: list = []
    monkeypatch.setattr(server, "write_json", lambda obj: captured.append(obj) or True)
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid: None)

    session = _session(agent=None, running=False)
    server._sessions["sid"] = session
    try:
        submit = server.handle_request(
            {"id": "1", "method": "prompt.submit", "params": {"session_id": "sid", "text": "hello"}}
        )
        assert submit.get("result"), f"got error: {submit.get('error')}"
        ack_turn_id = submit["result"]["turn_id"]
        assert ack_turn_id

        stop = server.handle_request(
            {"id": "2", "method": "session.interrupt", "params": {"session_id": "sid"}}
        )
        assert stop.get("result")

        threads[0].target()

        errors = _events(captured, "error", sid="sid")
        assert len(errors) == 1
        assert errors[0]["turn_id"] == ack_turn_id
    finally:
        server._sessions.pop("sid", None)


# ── Ordering: ack is constructed/returned before the turn thread runs ──


def test_ack_available_before_turn_thread_runs(monkeypatch):
    """Structural proof of the documented ack-before-events property: the
    RPC response is fully built and returned to the caller synchronously,
    strictly before the turn thread (which emits the first event) is ever
    invoked. Using a thread stub that requires an explicit manual
    ``.target()`` call (instead of running inline) makes the two steps
    observably ordered rather than coincidentally so."""
    threads = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            threads.append(self)

        def start(self):
            return None

        def is_alive(self):
            return True

    captured: list = []
    monkeypatch.setattr(server, "write_json", lambda obj: captured.append(obj) or True)
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda session, rid, sid: None)

    agent = types.SimpleNamespace(
        session_id="session-key",
        run_conversation=lambda *a, **k: {"final_response": "done"},
        clear_interrupt=lambda: None,
    )
    session = _session(agent=agent, running=False)
    server._sessions["sid"] = session
    try:
        resp = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "hello"}
        )
        # The ack is already fully formed here...
        assert resp["result"]["status"] == "streaming"
        turn_id = resp["result"]["turn_id"]
        assert turn_id
        # ...and no event for this turn has been written yet: the thread
        # that would emit message.start has not run.
        assert _events(captured, "message.start", sid="sid") == []

        threads[0].target()

        starts = _events(captured, "message.start", sid="sid")
        assert starts and starts[0]["turn_id"] == turn_id
    finally:
        server._sessions.pop("sid", None)


# ── Session-level events stay valid without a turn id ──────────────────


def test_session_level_event_has_no_turn_id_key(frames):
    server._emit("session.info", "sid", {"model": "x"})

    events = _events(frames, "session.info", sid="sid")
    assert len(events) == 1
    assert "turn_id" not in events[0]
    # Old-client shape is untouched: type/session_id/payload only.
    assert set(events[0].keys()) == {"type", "session_id", "payload"}


def test_bare_event_with_no_payload_has_no_turn_id_key(frames):
    server._emit("message.start", "sid")

    events = _events(frames, "message.start", sid="sid")
    assert len(events) == 1
    assert set(events[0].keys()) == {"type", "session_id"}


def test_event_inside_turn_scope_is_additive_only(frames):
    """Turn attribution adds exactly one field; nothing else about the
    frame shape changes."""
    with server._turn_scope("fixed-turn-id"):
        server._emit("message.delta", "sid", {"text": "hi"})

    events = _events(frames, "message.delta", sid="sid")
    assert events[0]["turn_id"] == "fixed-turn-id"
    assert set(events[0].keys()) == {"type", "session_id", "payload", "turn_id"}


# ── No turn id ever reaches the model ───────────────────────────────────


def test_turn_id_never_enters_model_facing_payload(turn_env, frames):
    seen_run_kwargs = {}

    def _run(message, **kwargs):
        seen_run_kwargs["message"] = message
        seen_run_kwargs["kwargs"] = kwargs
        return {"final_response": "ok"}

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None
    )
    session = _session(
        agent=agent,
        running=True,
        history=[{"role": "user", "content": "earlier turn"}],
    )

    server._run_prompt_submit("rid", "sid", session, "hello there")

    message = seen_run_kwargs["message"]
    assert "turn_id" not in str(message)
    history_kwarg = seen_run_kwargs["kwargs"].get("conversation_history")
    assert history_kwarg is not None
    for entry in history_kwarg:
        assert "turn_id" not in entry

    completes = _events(frames, "message.complete", sid="sid")
    turn_id = completes[0]["turn_id"]
    # Sanity: the id really was generated (looks nothing like the prompt
    # text) and definitely isn't sitting in the persisted prompt either.
    assert turn_id not in str(message)
    assert session["history"] == [{"role": "user", "content": "earlier turn"}] or all(
        turn_id not in str(m) for m in session["history"]
    )


# ── Reconnect / resume retains correlation without mutating history ────


def test_resume_snapshot_never_mints_a_new_id(turn_env, frames):
    """_inflight_snapshot (session.resume's live payload) must hand back the
    SAME id a still-connected client is correlating against — not a fresh
    one — and must not touch history/messages to do it."""
    seen = {}

    def _run(message, stream_callback=None, **kwargs):
        stream_callback("partial")
        seen["mid_turn_snapshot"] = server._inflight_snapshot(session)
        return {"final_response": "partial done"}

    agent = types.SimpleNamespace(
        session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None
    )
    session = _session(agent=agent, running=True)

    server._run_prompt_submit("rid", "sid", session, "hi")

    starts = _events(frames, "message.start", sid="sid")
    mid_turn = seen["mid_turn_snapshot"]
    assert mid_turn is not None
    assert mid_turn["turn_id"] == starts[0]["turn_id"]
    # A completed (non-error) turn's snapshot is cleared, same as before
    # this change — resume correlation only applies to the LIVE turn.
    assert server._inflight_snapshot(session) is None
