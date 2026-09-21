"""Zippy iOS voice fast path: session.resume per-call overrides.

Extends the existing per-session ``model``/``provider``/``reasoning_effort``/
``fast`` override contract (already proven for ``session.create`` in
``test_zippy_voice_session_create.py``) to ``session.resume``, for both a
fresh/deferred agent build and a resume that lands on an already-live agent:

* an explicit call param wins over the chat's previously-used (stored)
  runtime identity, which in turn wins over the profile default;
* omitting the new params reproduces byte-identical behavior to before this
  mechanism existed (the plain ``_stored_session_runtime_overrides`` restore);
* an invalid ``reasoning_effort`` fails safe — the prior value survives,
  it is not blanked;
* none of this ever writes to global config.yaml;
* a resume that lands on an ALREADY-LIVE session applies the override
  in place using the same primitives a live ``/model``/``/reasoning``/``/fast``
  change already uses (no system-prompt rebuild, no toolset swap), stashes a
  running turn's model switch for the next turn boundary instead of doing it
  unsafely, and rejects a ``source`` change outright (the platform-hint block
  is baked into the system prompt at build time and must stay stable for the
  life of the conversation).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tui_gateway.server as server

STORED_ROW = {
    "id": "s1",
    "cwd": "",
    "message_count": 0,
    "model": "stored-model",
    "model_config": {
        "provider": "stored-provider",
        "reasoning_config": {"enabled": True, "effort": "high"},
        "service_tier": "normal",
    },
}


class _RecordingDB:
    """Minimal stand-in for ``hermes_state.SessionDB`` covering what
    ``session.resume`` touches (see test_session_resume_db_ownership.py)."""

    def __init__(self, db_path=None, **_kwargs):
        self.db_path = db_path
        self.closed = 0
        self.rows: dict = {}

    def close(self):
        self.closed += 1

    def get_session(self, target):
        return self.rows.get(target)

    def get_session_by_title(self, _target):
        return None

    def resolve_resume_session_id(self, target):
        return target

    def reopen_session(self, _target):
        pass

    def get_resume_conversations(self, _target):
        return ([], [])

    def get_ancestor_display_prefix(self, _target):
        return []

    def get_messages_as_conversation(self, _target, **_kwargs):
        return []


@pytest.fixture()
def resume_db(monkeypatch, tmp_path):
    profile_home = tmp_path / "work"
    profile_home.mkdir()
    opened: list[_RecordingDB] = []

    def _factory(db_path=None, **kwargs):
        db = _RecordingDB(db_path=db_path, **kwargs)
        db.rows["s1"] = dict(STORED_ROW)
        opened.append(db)
        return db

    monkeypatch.setattr("hermes_state.SessionDB", _factory)
    monkeypatch.setattr(
        server, "_profile_home", lambda profile: profile_home if profile else None
    )
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    known = set(server._sessions)
    yield opened
    with server._sessions_lock:
        for sid in [s for s in server._sessions if s not in known]:
            server._sessions.pop(sid, None)


def _resume(**params):
    params.setdefault("profile", "work")
    return server.handle_request(
        {"id": "1", "method": "session.resume", "params": params}
    )


class TestColdResumeVoiceOverrides:
    """Explicit call params win over the stored row on every deferred branch."""

    def test_non_deferred_cold_resume_honors_call_params(self, resume_db):
        resp = _resume(
            session_id="s1",
            source="zippy_voice",
            model="gemini-3-flash-preview",
            provider="gemini",
            reasoning_effort="low",
            fast=True,
        )
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["source"] == "zippy_voice"
        assert sess["model_override"] == {
            "model": "gemini-3-flash-preview",
            "provider": "gemini",
        }
        overrides = sess["resume_runtime_overrides"]
        assert overrides["provider_override"] == "gemini"
        assert overrides["reasoning_config_override"] == {
            "enabled": True,
            "effort": "low",
        }
        assert overrides["service_tier_override"] == "priority"
        assert resp["result"]["info"]["model"] == "gemini-3-flash-preview"
        assert resp["result"]["info"]["provider"] == "gemini"

    def test_defer_history_branch_honors_call_params(self, resume_db):
        resp = _resume(
            session_id="s1",
            defer_history=True,
            model="gpt-5.6-sol",
            provider="openai-codex",
        )
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["model_override"] == {
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
        }
        assert resp["result"]["info"]["model"] == "gpt-5.6-sol"
        assert resp["result"]["info"]["provider"] == "openai-codex"

    def test_lazy_branch_honors_call_params_from_empty_base(self, resume_db):
        resp = _resume(session_id="s1", lazy=True, model="claude-fable-5-1")
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["model_override"] == {
            "model": "claude-fable-5-1",
            "provider": None,
        }
        assert resp["result"]["info"]["model"] == "claude-fable-5-1"


class TestOmittedParamsUnchanged:
    def test_cold_resume_without_new_params_matches_stored_overrides_exactly(
        self, resume_db
    ):
        expected = server._stored_session_runtime_overrides(dict(STORED_ROW))
        resp = _resume(session_id="s1")
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["resume_runtime_overrides"] == expected
        assert sess["model_override"] == expected.get("model_override")


class TestInvalidReasoningEffortFailsSafe:
    def test_invalid_value_preserves_stored_reasoning(self, resume_db):
        resp = _resume(session_id="s1", reasoning_effort="not-a-real-level")
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["resume_runtime_overrides"]["reasoning_config_override"] == {
            "enabled": True,
            "effort": "high",
        }
        assert "error" not in resp


class TestNoConfigWrite:
    def test_voice_resume_never_writes_config(self, resume_db, monkeypatch):
        writes = []
        monkeypatch.setattr(
            server, "_write_config_key", lambda *a, **k: writes.append((a, k))
        )
        try:
            import hermes_cli.config as config_mod

            monkeypatch.setattr(
                config_mod,
                "save_config",
                lambda *a, **k: writes.append((a, k)),
                raising=False,
            )
        except Exception:
            pass
        _resume(
            session_id="s1",
            source="zippy_voice",
            model="gpt-5.6-sol",
            provider="openai-codex",
            reasoning_effort="minimal",
            fast=True,
        )
        assert writes == []


class TestLiveTakeover:
    """A resume that hits the already-live fast path mutates in place, using
    the same primitives a manual /model, /reasoning, /fast change would."""

    def _live_session(self, *, running=False, source="tui"):
        agent = SimpleNamespace(
            model="stored-model",
            provider="stored-provider",
            reasoning_config={"enabled": True, "effort": "high"},
            service_tier=None,
            request_overrides={},
            session_id="s1",
        )
        session = {
            "session_key": "s1",
            "agent": agent,
            "running": running,
            "source": source,
            "model_override": None,
            "create_reasoning_override": None,
            "create_service_tier_override": None,
        }
        return agent, session

    def _mock_live_payload(self, monkeypatch):
        def _payload(sid, session, **_kwargs):
            agent = session.get("agent")
            return {
                "session_id": sid,
                "info": {
                    "model": getattr(agent, "model", None),
                    "provider": getattr(agent, "provider", None),
                    "reasoning_effort": (
                        (getattr(agent, "reasoning_config", None) or {}).get("effort")
                    ),
                    "service_tier": getattr(agent, "service_tier", None),
                },
                "pending_model_switch": session.get("pending_model_switch"),
            }

        monkeypatch.setattr(server, "_live_session_payload", _payload)

    def test_not_running_applies_reasoning_and_fast_in_place(
        self, resume_db, monkeypatch
    ):
        agent, session = self._live_session(running=False)
        monkeypatch.setattr(
            server, "_find_live_session_by_key", lambda _key: ("live-sid", session)
        )
        monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *a, **k: None)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        self._mock_live_payload(monkeypatch)

        resp = _resume(session_id="s1", reasoning_effort="low", fast=True)

        assert agent.reasoning_config == {"enabled": True, "effort": "low"}
        assert agent.service_tier == "priority"
        assert resp["result"]["info"]["reasoning_effort"] == "low"
        assert resp["result"]["info"]["service_tier"] == "priority"

    def test_not_running_model_override_goes_through_session_scoped_switch(
        self, resume_db, monkeypatch
    ):
        agent, session = self._live_session(running=False)
        monkeypatch.setattr(
            server, "_find_live_session_by_key", lambda _key: ("live-sid", session)
        )
        monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *a, **k: None)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        self._mock_live_payload(monkeypatch)
        calls = []
        monkeypatch.setattr(
            server,
            "_apply_model_switch",
            lambda *a, **k: calls.append((a, k)) or {"value": "gemini-3-flash-preview"},
        )

        resp = _resume(session_id="s1", model="gemini-3-flash-preview", provider="gemini")

        assert len(calls) == 1
        _args, kwargs = calls[0]
        # persist_override=False is load-bearing: without it a bare /model
        # switch can fall through to model.persist_switch_by_default and
        # silently write config.yaml globally.
        assert kwargs["persist_override"] is False
        assert kwargs["confirm_expensive_model"] is True
        assert kwargs["parsed_flags"].target == "gemini-3-flash-preview"
        assert kwargs["parsed_flags"].explicit_provider == "gemini"
        assert "error" not in resp

    def test_running_session_defers_model_switch_to_next_turn(
        self, resume_db, monkeypatch
    ):
        agent, session = self._live_session(running=True)
        monkeypatch.setattr(
            server, "_find_live_session_by_key", lambda _key: ("live-sid", session)
        )
        monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *a, **k: None)
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        self._mock_live_payload(monkeypatch)
        called = []
        monkeypatch.setattr(
            server, "_apply_model_switch", lambda *a, **k: called.append(1)
        )

        resp = _resume(session_id="s1", model="gemini-3-flash-preview")

        assert called == []
        assert session["pending_model_switch"]["display_model"] == "gemini-3-flash-preview"
        assert "error" not in resp
        # The live agent itself is untouched until the next turn boundary.
        assert agent.model == "stored-model"

    def test_source_change_on_live_session_is_rejected_and_nothing_mutated(
        self, resume_db, monkeypatch
    ):
        agent, session = self._live_session(running=False, source="tui")
        monkeypatch.setattr(
            server, "_find_live_session_by_key", lambda _key: ("live-sid", session)
        )
        persisted = []
        monkeypatch.setattr(
            server, "_persist_live_session_runtime", lambda *a, **k: persisted.append(1)
        )
        emitted = []
        monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(1))
        self._mock_live_payload(monkeypatch)

        resp = _resume(session_id="s1", source="zippy_voice", model="gemini-3-flash-preview")

        assert resp["error"]["code"] == 4131
        assert agent.model == "stored-model"
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert session["model_override"] is None
        assert persisted == []
        assert emitted == []

    def test_no_override_params_is_a_pure_noop(self, resume_db, monkeypatch):
        agent, session = self._live_session(running=False)
        monkeypatch.setattr(
            server, "_find_live_session_by_key", lambda _key: ("live-sid", session)
        )
        persisted = []
        monkeypatch.setattr(
            server, "_persist_live_session_runtime", lambda *a, **k: persisted.append(1)
        )
        emitted = []
        monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(1))
        self._mock_live_payload(monkeypatch)

        resp = _resume(session_id="s1")

        assert persisted == []
        assert emitted == []
        assert resp["result"]["info"]["model"] == "stored-model"
