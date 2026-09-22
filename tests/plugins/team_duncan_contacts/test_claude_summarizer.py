"""Tests for the OPS-110 claude-max subprocess summarizer.

No real subprocess is ever spawned: every test injects a fake `runner`
callable, matching the LiveDeskTransport precedent (call_history_collector.py)
of an injectable runner seam around subprocess.run. The fake runner plays
claude-max's role of writing output.json into the temp dir it's told about.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from plugins.team_duncan_contacts.claude_summarizer import (
    CONTACT_TYPES,
    MAX_OUTPUT_BYTES,
    SummarizerError,
    run_claude_summary,
)
from plugins.team_duncan_contacts import claude_summarizer as claude_summarizer_module

VALID_PAYLOAD: dict[str, Any] = {
    "contact_type": "agent_partner",
    "summary_lines": [
        "Clay and Cory discussed a new listing referral.",
        "Cory asked about commission split on future co-listed deals.",
    ],
    "discussed": "A potential referral and commission split arrangement.",
    "clay_commitment": "Clay will send the standard co-listing agreement.",
    "next_step": "Cory will review the agreement and reply by Friday.",
}


def _extract_prompt(argv: list[str]) -> str:
    assert "-p" in argv, "argv must pass the prompt via -p"
    return argv[argv.index("-p") + 1]


def _extract_output_path(argv: list[str]) -> Path:
    prompt = _extract_prompt(argv)
    match = re.search(r"(\S+/output\.json)", prompt)
    assert match, "fixed prompt must reference the output.json path"
    return Path(match.group(1))


class RecordingRunner:
    """Fake claude-max: writes *payload* to the output path the prompt
    names, and records file modes/argv for inspection."""

    def __init__(
        self, payload: dict[str, Any] | None = VALID_PAYLOAD, *, returncode: int = 0,
        write_output: bool = True, raw_output: str | None = None,
    ) -> None:
        self.payload = payload
        self.returncode = returncode
        self.write_output = write_output
        self.raw_output = raw_output
        self.argv: list[str] | None = None
        self.tmpdir_mode: int | None = None
        self.transcript_mode: int | None = None
        self.context_mode: int | None = None
        self.tmpdir: Path | None = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.argv = argv
        output_path = _extract_output_path(argv)
        self.tmpdir = output_path.parent
        transcript_path = self.tmpdir / "transcript.json"
        context_path = self.tmpdir / "context.json"
        self.tmpdir_mode = stat.S_IMODE(self.tmpdir.stat().st_mode)
        self.transcript_mode = stat.S_IMODE(transcript_path.stat().st_mode)
        self.context_mode = stat.S_IMODE(context_path.stat().st_mode)
        if self.write_output:
            content = self.raw_output if self.raw_output is not None else json.dumps(self.payload)
            output_path.write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.returncode)


def _run(runner, **overrides):
    kwargs = dict(
        transcript_segments=[{"speaker": "Clay", "text": "hi"}],
        contact_context={"contact_id": "c-1", "type": "agent_partner"},
        hermes_home=Path("/nonexistent/hermes-home"),
        runner=runner,
    )
    kwargs.update(overrides)
    return run_claude_summary(**kwargs)


class TestHappyPath:
    def test_valid_output_parses_into_summary_result(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        result = _run(runner)
        assert result.contact_type == "agent_partner"
        assert result.summary_lines == VALID_PAYLOAD["summary_lines"]
        assert result.discussed == VALID_PAYLOAD["discussed"]
        assert result.clay_commitment == VALID_PAYLOAD["clay_commitment"]
        assert result.next_step == VALID_PAYLOAD["next_step"]

    def test_none_stated_is_accepted_for_commitment_and_next_step(self) -> None:
        payload = {**VALID_PAYLOAD, "clay_commitment": "None stated.", "next_step": "None stated."}
        result = _run(RecordingRunner(payload))
        assert result.clay_commitment == "None stated."
        assert result.next_step == "None stated."

    def test_two_line_summary_is_accepted(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["Line one here.", "Line two here."]}
        result = _run(RecordingRunner(payload))
        assert len(result.summary_lines) == 2

    def test_three_line_summary_is_accepted(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["Line one.", "Line two.", "Line three."]}
        result = _run(RecordingRunner(payload))
        assert len(result.summary_lines) == 3

    @pytest.mark.parametrize("contact_type", CONTACT_TYPES)
    def test_every_contact_type_category_is_accepted(self, contact_type: str) -> None:
        payload = {**VALID_PAYLOAD, "contact_type": contact_type}
        result = _run(RecordingRunner(payload))
        assert result.contact_type == contact_type


class TestFilePermissionsAndCleanup:
    def test_temp_dir_is_mode_0700(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        assert runner.tmpdir_mode == 0o700

    def test_input_files_are_mode_0600(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        assert runner.transcript_mode == 0o600
        assert runner.context_mode == 0o600

    def test_temp_dir_is_outside_the_repo(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        repo_root = Path(__file__).resolve().parents[3]
        assert repo_root not in runner.tmpdir.parents

    def test_temp_dir_is_removed_after_success(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        assert not runner.tmpdir.exists()

    def test_temp_dir_is_removed_after_failure(self) -> None:
        runner = RecordingRunner(returncode=1)
        with pytest.raises(SummarizerError):
            _run(runner)
        assert not runner.tmpdir.exists()


class TestSubprocessFailureModes:
    def test_nonzero_exit_raises_content_free_error(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(returncode=1))
        assert exc_info.value.error_class == "nonzero_exit"

    def test_timeout_raises_content_free_error(self) -> None:
        def _timeout_runner(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        with pytest.raises(SummarizerError) as exc_info:
            _run(_timeout_runner)
        assert exc_info.value.error_class == "timeout"

    def test_subprocess_launch_failure_raises_content_free_error(self) -> None:
        def _broken_runner(argv):
            raise OSError("no such file")

        with pytest.raises(SummarizerError) as exc_info:
            _run(_broken_runner)
        assert exc_info.value.error_class == "subprocess_failed_to_execute"

    def test_missing_output_file_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(write_output=False))
        assert exc_info.value.error_class == "no_output_file"

    def test_empty_output_file_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(raw_output=""))
        assert exc_info.value.error_class == "empty_output_file"

    def test_oversized_output_file_raises_without_reading_it(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(raw_output="x" * (MAX_OUTPUT_BYTES + 1)))
        assert exc_info.value.error_class == "output_too_large"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(raw_output="not json at all"))
        assert exc_info.value.error_class == "invalid_json"


class TestStrictSchemaValidation:
    def test_missing_keys_rejected(self) -> None:
        payload = dict(VALID_PAYLOAD)
        del payload["clay_commitment"]
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "missing_keys"

    def test_invalid_contact_type_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "contact_type": "mortgage_prospect"}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_contact_type"

    def test_one_line_summary_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["only one line here."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_four_line_summary_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["a.", "b.", "c.", "d."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_empty_summary_line_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["", "second line."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_markdown_bullet_in_summary_line_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["- a bullet point.", "second line."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_url_in_summary_line_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["See https://example.com for details.", "second."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_raw_phone_number_in_summary_line_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": ["Call 555-123-4567 to confirm.", "second."]}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_empty_clay_commitment_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "clay_commitment": ""}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_clay_commitment"

    def test_empty_next_step_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "next_step": ""}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_next_step"

    def test_non_list_summary_lines_rejected(self) -> None:
        payload = {**VALID_PAYLOAD, "summary_lines": "just a string"}
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_non_dict_payload_rejected(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(raw_output=json.dumps(["not", "a", "dict"])))
        assert exc_info.value.error_class == "invalid_json_shape"


class TestNonInteractivePermissionGrant:
    """Non-interactive claude-max has no permission to write the fixed
    output path unless explicitly granted -- but only the exact, minimal
    grant the fixed prompt needs, never anything broader."""

    def test_argv_grants_exactly_read_write_non_interactively(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        prompt = _extract_prompt(runner.argv)
        assert runner.argv == [
            runner.argv[0],
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--allowedTools",
            "Read,Write",
        ]

    def test_no_other_tools_are_granted(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        allowed_index = runner.argv.index("--allowedTools")
        allowed_value = runner.argv[allowed_index + 1]
        assert allowed_value == "Read,Write"
        assert "Bash" not in runner.argv
        assert "Edit" not in runner.argv
        for forbidden in ("Bash", "Edit", "WebFetch", "WebSearch", "computer_use"):
            assert forbidden not in allowed_value

    def test_argv_is_never_shelled_out(self) -> None:
        # _default_runner must invoke subprocess.run with a fixed argv list
        # and shell=False (the default) -- never a shell string.
        import inspect

        source = inspect.getsource(claude_summarizer_module._default_runner)
        assert "shell=True" not in source


class TestOutputPathContainment:
    def test_output_path_escaping_temp_dir_fails_closed(self, monkeypatch, tmp_path) -> None:
        fake_tmpdir = tmp_path / "ops110-plaud-fake"
        fake_tmpdir.mkdir()
        monkeypatch.setattr(
            claude_summarizer_module.tempfile, "mkdtemp", lambda prefix: str(fake_tmpdir)
        )

        real_resolve = Path.resolve

        def _fake_resolve(self, *args, **kwargs):
            if self.name == "output.json":
                return Path("/elsewhere/output.json")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _fake_resolve)

        runner = RecordingRunner(VALID_PAYLOAD)
        with pytest.raises(SummarizerError) as exc_info:
            _run(runner)
        assert exc_info.value.error_class == "unsafe_temp_path"
        assert runner.argv is None, "subprocess must never launch when the path check fails"

    def test_transcript_and_context_paths_also_checked(self, monkeypatch, tmp_path) -> None:
        fake_tmpdir = tmp_path / "ops110-plaud-fake-2"
        fake_tmpdir.mkdir()
        monkeypatch.setattr(
            claude_summarizer_module.tempfile, "mkdtemp", lambda prefix: str(fake_tmpdir)
        )

        real_resolve = Path.resolve

        def _fake_resolve(self, *args, **kwargs):
            if self.name == "transcript.json":
                return Path("/elsewhere/transcript.json")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _fake_resolve)

        runner = RecordingRunner(VALID_PAYLOAD)
        with pytest.raises(SummarizerError) as exc_info:
            _run(runner)
        assert exc_info.value.error_class == "unsafe_temp_path"
        assert runner.argv is None

    def test_happy_path_output_resolves_inside_temp_dir(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        _run(runner)
        output_path = _extract_output_path(runner.argv)
        assert output_path.parent == runner.tmpdir


class TestRealShapedSubprocessIntegration:
    """Exercises the real, unmocked _default_runner -- a genuine local
    subprocess.run call against a fake claude-max stand-in -- reproducing
    the shape of production execution rather than an injected callable."""

    def test_default_runner_spawns_real_subprocess_and_parses_written_output(
        self, tmp_path
    ) -> None:
        hermes_home = tmp_path / "hermes-home"
        bin_dir = hermes_home / "bin"
        bin_dir.mkdir(parents=True)
        fake_claude_max = bin_dir / "claude-max"
        argv_marker = tmp_path / "argv_seen.txt"
        fake_claude_max.write_text(
            "#!/bin/sh\n"
            'echo "$@" > "' + str(argv_marker) + '"\n'
            'prompt="$2"\n'
            "output_path=$(printf '%s' \"$prompt\" | grep -o '[^ ]*output\\.json' | head -1)\n"
            'cat > "$output_path" <<JSON\n'
            + json.dumps(VALID_PAYLOAD) + "\n"
            "JSON\n"
            "exit 0\n"
        )
        fake_claude_max.chmod(0o700)

        result = run_claude_summary(
            transcript_segments=[{"speaker": "Clay", "text": "hi"}],
            contact_context={"contact_id": "c-1", "type": "agent_partner"},
            hermes_home=hermes_home,
        )

        assert result.contact_type == VALID_PAYLOAD["contact_type"]
        assert result.summary_lines == VALID_PAYLOAD["summary_lines"]
        assert result.discussed == VALID_PAYLOAD["discussed"]

        argv_seen = argv_marker.read_text(encoding="utf-8")
        assert "--dangerously-skip-permissions" in argv_seen
        assert "--allowedTools Read,Write" in argv_seen


class TestNoStdoutFallback:
    def test_valid_json_on_stdout_does_not_rescue_a_missing_output_file(self) -> None:
        def _runner(argv):
            return subprocess.CompletedProcess(
                argv,
                returncode=0,
                stdout=json.dumps(VALID_PAYLOAD).encode("utf-8"),
                stderr=b"",
            )

        with pytest.raises(SummarizerError) as exc_info:
            _run(_runner)
        assert exc_info.value.error_class == "no_output_file"


class TestAttributionSafeguard:
    """The summarizer keeps discussed/clay_commitment/next_step and
    summary_lines entirely separate fields -- proving there is no code path
    here that reconstructs or merges one into another (which is what would
    risk attributing one person's commitment to the other)."""

    def test_distinct_fields_are_never_merged_or_swapped(self) -> None:
        payload = {
            **VALID_PAYLOAD,
            "clay_commitment": "Clay will send the comps by end of day.",
            "next_step": "Cory will follow up with the seller next week.",
        }
        result = _run(RecordingRunner(payload))
        assert result.clay_commitment == "Clay will send the comps by end of day."
        assert result.next_step == "Cory will follow up with the seller next week."
        assert result.clay_commitment not in result.summary_lines
        assert result.next_step not in result.summary_lines
