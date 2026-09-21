"""Zippy iOS voice fast path: session.create source isolation.

Covers the contract for the ``zippy_voice`` session source added for the
iOS voice client:

* it is recognized purely as a value of the existing ``source`` param
  (no new RPC method, no allow-list to update) and flows to ``agent.platform``
  exactly like ``desktop``/``tui``/``cli`` already do;
* it gets the SAME toolset as an ordinary tui/desktop session (no
  fragmentation of the tool surface for voice);
* the existing per-session model/provider/reasoning_effort/fast overrides
  already wired into ``session.create`` work unchanged for this source;
* none of this writes to global config, and sessions created with any other
  (or no) source are completely unaffected.
"""

import pytest

import tui_gateway.server as server


@pytest.fixture(autouse=True)
def _clear_sessions():
    server._sessions.clear()
    yield
    server._sessions.clear()


def _create(monkeypatch, rid, params):
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    return server._methods["session.create"](rid, params)


class TestZippyVoiceSourceRecognition:
    def test_source_is_stored_verbatim_and_not_rejected(self, monkeypatch):
        resp = _create(monkeypatch, "r1", {"cols": 80, "source": "zippy_voice"})
        sid = resp["result"]["session_id"]
        assert server._sessions[sid]["source"] == "zippy_voice"

    def test_resolves_to_the_same_agent_platform_tui_desktop_use(self):
        assert server._resolve_agent_platform("zippy_voice") == "zippy_voice"

    def test_gets_identical_toolset_to_a_plain_tui_session(self, monkeypatch):
        monkeypatch.delenv("HERMES_DESKTOP", raising=False)
        monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
        monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
        import agent.coding_context as cc

        monkeypatch.setattr(cc, "coding_selection", lambda **_: None)
        assert server._load_enabled_toolsets(
            "zippy_voice"
        ) == server._load_enabled_toolsets("tui")

    def test_does_not_pick_up_desktop_only_tools(self, monkeypatch):
        monkeypatch.delenv("HERMES_DESKTOP", raising=False)
        monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
        assert "desktop_ui" not in server._gui_surface_toolsets("zippy_voice")


class TestZippyVoiceModelReasoningOverridesHonored:
    """Same per-session override contract as
    ``test_session_create_records_ui_model_as_session_override``, just with
    ``source="zippy_voice"``, the iOS client's exact session.create shape."""

    def test_model_provider_reasoning_and_fast_are_recorded_as_session_overrides(
        self, monkeypatch
    ):
        resp = _create(
            monkeypatch,
            "r1",
            {
                "cols": 80,
                "source": "zippy_voice",
                "model": "gemini-3-flash-preview",
                "provider": "gemini",
                "reasoning_effort": "low",
                "fast": True,
            },
        )
        sid = resp["result"]["session_id"]
        sess = server._sessions[sid]
        assert sess["source"] == "zippy_voice"
        assert sess["model_override"] == {
            "model": "gemini-3-flash-preview",
            "provider": "gemini",
        }
        assert sess["create_reasoning_override"] == {"enabled": True, "effort": "low"}
        assert sess["create_service_tier_override"] == "priority"
        assert resp["result"]["info"]["model"] == "gemini-3-flash-preview"
        assert resp["result"]["info"]["provider"] == "gemini"

    def test_no_knobs_means_inherit_profile_default(self, monkeypatch):
        resp = _create(monkeypatch, "r1", {"cols": 80, "source": "zippy_voice"})
        sess = server._sessions[resp["result"]["session_id"]]
        assert sess["model_override"] is None
        assert sess["create_reasoning_override"] is None
        assert sess["create_service_tier_override"] is None


class TestOtherSourcesUnaffected:
    """Adding zippy_voice must not change behavior for any existing source."""

    @pytest.mark.parametrize("source", ["desktop", "tui", "cli", "telegram", None])
    def test_existing_sources_still_resolve_to_themselves_or_platform_default(
        self, source
    ):
        resolved = server._resolve_session_source(source)
        if source:
            assert resolved == source
        else:
            assert resolved in ("tui", "desktop")

    def test_plain_session_create_without_source_is_unchanged(self, monkeypatch):
        resp = _create(monkeypatch, "r1", {"cols": 80})
        sess = server._sessions[resp["result"]["session_id"]]
        assert sess["source"] != "zippy_voice"
        assert sess["model_override"] is None

    def test_config_is_never_written_by_a_zippy_voice_create(self, monkeypatch):
        writes = []
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
        _create(
            monkeypatch,
            "r1",
            {
                "cols": 80,
                "source": "zippy_voice",
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "reasoning_effort": "minimal",
            },
        )
        assert writes == []
