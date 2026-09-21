"""System-prompt assembly for the Zippy iOS voice fast path.

Covers the new ``PLATFORM_HINTS["zippy_voice"]`` entry: it must be a
strictly voice-scoped, cache-stable addition that never leaks into (or
changes) the prompt any other platform/source builds.

These tests run against the real prompt builders (no mocks) because
cache-stability and byte-for-byte text contracts are what we are
verifying; mocking the resolver would hide exactly the class of bug
this test covers.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_builder import PLATFORM_HINTS
from agent.system_prompt import build_system_prompt_parts


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _make_agent(platform="", **overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides={},
        model="",
        provider="",
        pass_session_id=False,
        session_id="",
    )
    base["platform"] = platform
    base.update(overrides)
    return SimpleNamespace(**base)


class TestZippyVoiceHintEntry:
    def test_zippy_voice_key_exists(self):
        """Without this entry the platform-hint lookup falls through to an
        empty string and the voice session gets no spoken-output framing."""
        assert "zippy_voice" in PLATFORM_HINTS

    def test_hint_forbids_markdown_and_urls(self):
        hint = PLATFORM_HINTS["zippy_voice"].lower()
        assert "no markdown" in hint
        assert "url" in hint

    def test_hint_bounds_retrieval_to_one_call(self):
        hint = PLATFORM_HINTS["zippy_voice"].lower()
        assert "at most one" in hint
        assert "session_search" in hint

    def test_hint_prefers_session_search_over_memory_recall_for_recency(self):
        hint = PLATFORM_HINTS["zippy_voice"]
        assert "session_search" in hint
        assert "memory-recall" in hint.lower()


class TestZippyVoiceHintResolutionInStablePrompt:
    def test_zippy_voice_platform_yields_voice_hint(self):
        stable = _stable_prompt(_make_agent(platform="zippy_voice"))
        assert PLATFORM_HINTS["zippy_voice"] in stable

    def test_zippy_voice_hint_is_cache_stable_across_rebuilds(self):
        agent = _make_agent(platform="zippy_voice")
        first = _stable_prompt(agent)
        second = _stable_prompt(agent)
        assert first == second


class TestZippyVoiceIsolatedFromOtherPlatforms:
    """Adding the voice source must not change any existing platform's
    prompt, and non-voice sessions must never pick up the voice hint."""

    def test_cli_prompt_unaffected(self):
        stable = _stable_prompt(_make_agent(platform="cli"))
        assert PLATFORM_HINTS["zippy_voice"] not in stable
        assert PLATFORM_HINTS["cli"] in stable

    def test_tui_prompt_unaffected(self):
        stable = _stable_prompt(_make_agent(platform="tui"))
        assert PLATFORM_HINTS["zippy_voice"] not in stable
        assert PLATFORM_HINTS["tui"] in stable

    def test_desktop_prompt_unaffected(self):
        stable = _stable_prompt(_make_agent(platform="desktop"))
        assert PLATFORM_HINTS["zippy_voice"] not in stable
        assert PLATFORM_HINTS["desktop"] in stable

    def test_unset_platform_gets_no_voice_hint(self):
        stable = _stable_prompt(_make_agent(platform=""))
        assert PLATFORM_HINTS["zippy_voice"] not in stable
