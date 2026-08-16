"""Tests for tts.elevenlabs.voice_settings config block.

Covers:
- When voice_settings is absent from config, no voice_settings kwarg is passed
  to client.text_to_speech.convert().
- When voice_settings is present, a VoiceSettings object constructed from
  the config dict is passed through.
"""
from unittest.mock import MagicMock, call, patch

import pytest

from tools import tts_tool


def _make_mock_elevenlabs_import(mock_client):
    """Return a factory mock that _import_elevenlabs() should return."""
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


def _base_config(**elevenlabs_overrides):
    el_cfg = {"voice_id": "test-voice", "model_id": "eleven_multilingual_v2"}
    el_cfg.update(elevenlabs_overrides)
    return {"provider": "elevenlabs", "elevenlabs": el_cfg}


class TestElevenLabsVoiceSettings:
    def test_no_voice_settings_in_config_omits_kwarg(self, tmp_path):
        """When voice_settings is absent, convert() must not receive voice_settings."""
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = iter([b"audio"])

        config = _base_config()  # no voice_settings key

        with patch.object(tts_tool, "get_env_value", return_value="test-api-key"), \
             patch.object(tts_tool, "_import_elevenlabs", return_value=_make_mock_elevenlabs_import(mock_client)), \
             patch.object(tts_tool, "_elevenlabs_environment_kwargs", return_value={}):
            output = str(tmp_path / "out.mp3")
            tts_tool._generate_elevenlabs("hello", output, config)

        _, kwargs = mock_client.text_to_speech.convert.call_args
        assert "voice_settings" not in kwargs

    def test_voice_settings_none_in_config_omits_kwarg(self, tmp_path):
        """When voice_settings is explicitly None (the DEFAULT_CONFIG sentinel), omit kwarg."""
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = iter([b"audio"])

        config = _base_config(voice_settings=None)

        with patch.object(tts_tool, "get_env_value", return_value="test-api-key"), \
             patch.object(tts_tool, "_import_elevenlabs", return_value=_make_mock_elevenlabs_import(mock_client)), \
             patch.object(tts_tool, "_elevenlabs_environment_kwargs", return_value={}):
            output = str(tmp_path / "out.mp3")
            tts_tool._generate_elevenlabs("hello", output, config)

        _, kwargs = mock_client.text_to_speech.convert.call_args
        assert "voice_settings" not in kwargs

    def test_voice_settings_passed_as_voice_settings_object(self, tmp_path):
        """When voice_settings dict is set, a VoiceSettings object is passed to convert()."""
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = iter([b"audio"])

        vs_dict = {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.1,
            "use_speaker_boost": True,
            "speed": 1.1,
        }
        config = _base_config(voice_settings=vs_dict)

        mock_voice_settings_cls = MagicMock()
        mock_voice_settings_instance = MagicMock()
        mock_voice_settings_cls.return_value = mock_voice_settings_instance

        with patch.object(tts_tool, "get_env_value", return_value="test-api-key"), \
             patch.object(tts_tool, "_import_elevenlabs", return_value=_make_mock_elevenlabs_import(mock_client)), \
             patch.object(tts_tool, "_elevenlabs_environment_kwargs", return_value={}), \
             patch("tools.tts_tool.VoiceSettings", mock_voice_settings_cls, create=True):
            # Patch the import inside the function by patching the module-level name
            # after monkey-patching builtins.__import__ via importlib trick.
            # Simpler: patch the VoiceSettings import directly at the call site.
            output = str(tmp_path / "out.mp3")
            # We need to intercept the `from elevenlabs.types.voice_settings import VoiceSettings`
            # that happens inside _generate_elevenlabs. Patch via sys.modules.
            import sys
            fake_vs_module = MagicMock()
            fake_vs_module.VoiceSettings = mock_voice_settings_cls
            with patch.dict(sys.modules, {"elevenlabs.types.voice_settings": fake_vs_module}):
                tts_tool._generate_elevenlabs("hello", output, config)

        mock_voice_settings_cls.assert_called_once_with(**vs_dict)
        _, kwargs = mock_client.text_to_speech.convert.call_args
        assert kwargs.get("voice_settings") is mock_voice_settings_instance

    def test_partial_voice_settings_passed_through(self, tmp_path):
        """Only the fields the user specifies are forwarded — no defaults injected."""
        mock_client = MagicMock()
        mock_client.text_to_speech.convert.return_value = iter([b"audio"])

        vs_dict = {"stability": 0.6}
        config = _base_config(voice_settings=vs_dict)

        mock_voice_settings_cls = MagicMock()
        mock_voice_settings_instance = MagicMock()
        mock_voice_settings_cls.return_value = mock_voice_settings_instance

        import sys
        fake_vs_module = MagicMock()
        fake_vs_module.VoiceSettings = mock_voice_settings_cls

        with patch.object(tts_tool, "get_env_value", return_value="test-api-key"), \
             patch.object(tts_tool, "_import_elevenlabs", return_value=_make_mock_elevenlabs_import(mock_client)), \
             patch.object(tts_tool, "_elevenlabs_environment_kwargs", return_value={}), \
             patch.dict(sys.modules, {"elevenlabs.types.voice_settings": fake_vs_module}):
            output = str(tmp_path / "out.mp3")
            tts_tool._generate_elevenlabs("hello", output, config)

        # VoiceSettings must be called with ONLY the keys the user supplied.
        mock_voice_settings_cls.assert_called_once_with(stability=0.6)
        _, kwargs = mock_client.text_to_speech.convert.call_args
        assert kwargs.get("voice_settings") is mock_voice_settings_instance
