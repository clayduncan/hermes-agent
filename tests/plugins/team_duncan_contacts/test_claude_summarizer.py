"""Tests for the OPS-110 claude-max subprocess summarizer.

No real subprocess is ever spawned: every test injects a fake `runner`
callable, matching the LiveDeskTransport precedent (call_history_collector.py)
of an injectable runner seam around subprocess.run. The fake runner plays
claude-max's role of writing output.json into the temp dir it's told about.

The visible note body is exactly Claude's validated, owner-labeled
summary_lines -- third person, no narrative lead-in on line 1, and every
remaining line labeled with exactly "Clay: " or the contact's exact
first_name label. discussed/clay_commitment/next_step are kept as a durable
record and cross-checked against summary_lines so the visible lines can
never silently omit or contradict a named commitment.
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
    SummaryResult,
    build_visible_body_lines,
    run_claude_summary,
)
from plugins.team_duncan_contacts import claude_summarizer as claude_summarizer_module

# Cory's exact example from the OPS-110 CRM summary-style correction.
CORY_LINE_1 = (
    "Referral and joint-venture opportunities with Realty One Group agents "
    "and brokers, centered on the Destin One Talks event and Clay's AI "
    "presentation tools."
)
CORY_CLAY_PHRASE = "covering his own costs to attend Destin, bringing prepared AI materials."
CORY_CONTACT_PHRASE = (
    "checking with the Destin broker owner on VIP cruise availability, "
    "following up in a day or two."
)

VALID_PAYLOAD: dict[str, Any] = {
    "contact_type": "agent_partner",
    "summary_lines": [
        CORY_LINE_1,
        f"Clay: {CORY_CLAY_PHRASE}",
        f"Cory: {CORY_CONTACT_PHRASE}",
    ],
    "discussed": CORY_LINE_1,
    "clay_commitment": CORY_CLAY_PHRASE,
    "next_step": CORY_CONTACT_PHRASE,
}

DEFAULT_CONTACT_CONTEXT: dict[str, Any] = {
    "contact_id": "c-1", "type": "agent_partner", "first_name": "Cory",
}


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    return {**VALID_PAYLOAD, **overrides}


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
        contact_context=dict(DEFAULT_CONTACT_CONTEXT),
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

    def test_clay_only_commitment_is_accepted_with_two_line_summary(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"],
            next_step="None stated.",
        )
        result = _run(RecordingRunner(payload))
        assert result.summary_lines == [CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"]
        assert result.next_step == "None stated."

    def test_contact_only_next_step_is_accepted_with_two_line_summary(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Cory: {CORY_CONTACT_PHRASE}"],
            clay_commitment="None stated.",
        )
        result = _run(RecordingRunner(payload))
        assert result.summary_lines == [CORY_LINE_1, f"Cory: {CORY_CONTACT_PHRASE}"]
        assert result.clay_commitment == "None stated."

    def test_three_line_summary_with_both_owners_is_accepted(self) -> None:
        result = _run(RecordingRunner(VALID_PAYLOAD))
        assert len(result.summary_lines) == 3

    @pytest.mark.parametrize("contact_type", CONTACT_TYPES)
    def test_every_contact_type_category_is_accepted(self, contact_type: str) -> None:
        payload = _valid_payload(contact_type=contact_type)
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


class TestMissingContactFirstName:
    """No visible owner label can ever be built for a contact whose
    first_name is missing -- this must fail closed before claude-max is
    even invoked."""

    def test_missing_first_name_key_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(VALID_PAYLOAD), contact_context={"contact_id": "c-1"})
        assert exc_info.value.error_class == "missing_contact_first_name"

    def test_blank_first_name_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(VALID_PAYLOAD), contact_context={"first_name": "   "})
        assert exc_info.value.error_class == "missing_contact_first_name"

    def test_non_string_first_name_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(VALID_PAYLOAD), contact_context={"first_name": None})
        assert exc_info.value.error_class == "missing_contact_first_name"

    def test_no_subprocess_is_launched(self) -> None:
        runner = RecordingRunner(VALID_PAYLOAD)
        with pytest.raises(SummarizerError):
            _run(runner, contact_context={"contact_id": "c-1"})
        assert runner.argv is None


class TestStrictSchemaValidation:
    def test_missing_keys_rejected(self) -> None:
        payload = dict(VALID_PAYLOAD)
        del payload["clay_commitment"]
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "missing_keys"

    def test_invalid_contact_type_rejected(self) -> None:
        payload = _valid_payload(contact_type="mortgage_prospect")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_contact_type"

    def test_one_line_summary_rejected(self) -> None:
        payload = _valid_payload(summary_lines=["only one line here."])
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_four_line_summary_rejected(self) -> None:
        payload = _valid_payload(summary_lines=["a.", "Clay: b.", "Cory: c.", "d."])
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_empty_summary_line_rejected(self) -> None:
        payload = _valid_payload(summary_lines=["", "Clay: sends it today."])
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_markdown_bullet_in_summary_line_rejected(self) -> None:
        payload = _valid_payload(summary_lines=["- a bullet point.", "second line."])
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_url_in_summary_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=["See https://example.com for details.", "second."]
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_raw_phone_number_in_summary_line_rejected(self) -> None:
        payload = _valid_payload(summary_lines=["Call 555-123-4567 to confirm.", "second."])
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_email_in_summary_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=["Email cory.vasquez@realtyonegroup.com for details.", "second."]
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_em_dash_in_summary_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[f"{CORY_LINE_1} — an aside.", f"Clay: {CORY_CLAY_PHRASE}"]
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_empty_clay_commitment_rejected(self) -> None:
        payload = _valid_payload(clay_commitment="")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_clay_commitment"

    def test_empty_next_step_rejected(self) -> None:
        payload = _valid_payload(next_step="")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_next_step"

    def test_non_list_summary_lines_rejected(self) -> None:
        payload = _valid_payload(summary_lines="just a string")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_non_dict_payload_rejected(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(raw_output=json.dumps(["not", "a", "dict"])))
        assert exc_info.value.error_class == "invalid_json_shape"


class TestNarrativeLeadInRejected:
    """Line 1 must open directly with the substance -- never narration."""

    @pytest.mark.parametrize(
        "leadin",
        [
            "Clay and Cory discussed a new listing referral.",
            "Clay discussed the referral opportunity.",
            "Cory and Clay talked about the referral.",
            "Discussed a new listing referral.",
        ],
    )
    def test_narrative_leadin_rejected(self, leadin: str) -> None:
        payload = _valid_payload(
            summary_lines=[leadin, f"Clay: {CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_non_narrative_line_mentioning_discussed_verb_midsentence_is_allowed(self) -> None:
        """The check targets a narration lead-in, not the mere presence of
        a verb like "discussed" -- a substantive line that only later
        references what was covered must still be accepted."""
        line1 = "A referral opportunity for a new listing, plus terms Cory discussed for future deals."
        payload = _valid_payload(
            summary_lines=[line1, f"Clay: {CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        result = _run(RecordingRunner(payload))
        assert result.summary_lines[0] == line1


class TestFirstPersonRejected:
    PRONOUNS = ["I", "me", "my", "mine", "we", "us", "our", "ours"]

    @pytest.mark.parametrize("pronoun", PRONOUNS)
    def test_first_person_pronoun_in_line_one_rejected(self, pronoun: str) -> None:
        line1 = f"The team reviewed {pronoun} notes about the referral opportunity."
        payload = _valid_payload(
            summary_lines=[line1, f"Clay: {CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    @pytest.mark.parametrize("pronoun", PRONOUNS)
    def test_first_person_pronoun_in_labeled_phrase_rejected(self, pronoun: str) -> None:
        # clay_commitment stays clean so this isolates the summary_lines
        # phrase check specifically, rather than the earlier field-level
        # clay_commitment check (which would otherwise raise first).
        phrase = f"shares {pronoun} notes with the team."
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay: {phrase}"],
            next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_first_person_pronoun_in_discussed_rejected(self) -> None:
        payload = _valid_payload(discussed="We discussed a new listing referral.")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_discussed"

    def test_first_person_pronoun_in_clay_commitment_rejected(self) -> None:
        payload = _valid_payload(clay_commitment="I will send the agreement.")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_clay_commitment"

    def test_first_person_pronoun_in_next_step_rejected(self) -> None:
        payload = _valid_payload(next_step="We will review the agreement.")
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_next_step"

    def test_word_boundary_does_not_false_positive_on_substrings(self) -> None:
        """Words like "hour", "trust", or "main" must not trip the
        first-person check -- only whole-word pronouns do."""
        line1 = "The main referral runs through trust in the hour before closing."
        payload = _valid_payload(
            summary_lines=[line1, f"Clay: {CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        result = _run(RecordingRunner(payload))
        assert result.summary_lines[0] == line1


class TestOwnerLabelValidation:
    def test_line_one_starting_with_clay_label_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[f"Clay: {CORY_CLAY_PHRASE}", CORY_LINE_1],
            next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_line_one_starting_with_contact_label_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[f"Cory: {CORY_CONTACT_PHRASE}", CORY_LINE_1],
            clay_commitment="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_unlabeled_commitment_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"{CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_wrong_name_label_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Bob: {CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_last_name_label_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Vasquez: {CORY_CONTACT_PHRASE}"],
            clay_commitment="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_duplicate_clay_owner_lines_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}", "Clay: also sends comps."],
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_duplicate_contact_owner_lines_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[
                CORY_LINE_1, f"Cory: {CORY_CONTACT_PHRASE}", "Cory: also checks pricing.",
            ],
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"

    def test_label_prefix_must_be_exact_with_one_space(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay:{CORY_CLAY_PHRASE}"], next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "invalid_summary_lines"


class TestAttributionCrossCheck:
    """The parser cross-checks that summary_lines cannot omit or
    contradict the durable clay_commitment/next_step fields."""

    def test_clay_commitment_stated_but_no_clay_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Cory: {CORY_CONTACT_PHRASE}"],
            clay_commitment=CORY_CLAY_PHRASE,
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_clay_line_present_but_clay_commitment_none_stated_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"],
            clay_commitment="None stated.",
            next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_next_step_stated_but_no_contact_line_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"],
            next_step=CORY_CONTACT_PHRASE,
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_contact_line_present_but_next_step_none_stated_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, f"Cory: {CORY_CONTACT_PHRASE}"],
            clay_commitment="None stated.",
            next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_clay_line_contradicts_clay_commitment_field_rejected(self) -> None:
        payload = _valid_payload(
            summary_lines=[CORY_LINE_1, "Clay: sends a completely unrelated market report."],
            clay_commitment=CORY_CLAY_PHRASE,
            next_step="None stated.",
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_mismatched_commitment_attribution_swap_rejected(self) -> None:
        """clay_commitment and next_step swapped relative to the visible
        lines -- each field names the *other* person's action."""
        payload = _valid_payload(
            summary_lines=[
                CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}", f"Cory: {CORY_CONTACT_PHRASE}",
            ],
            clay_commitment=CORY_CONTACT_PHRASE,
            next_step=CORY_CLAY_PHRASE,
        )
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload))
        assert exc_info.value.error_class == "commitment_attribution_mismatch"

    def test_commitment_and_next_step_are_wrapped_by_their_labeled_lines(self) -> None:
        """The happy path proves the invariant this whole class guards:
        the labeled line directly wraps ("Label: " + phrase) the durable
        field, rather than the two ever being independent."""
        result = _run(RecordingRunner(VALID_PAYLOAD))
        assert result.summary_lines[1] == f"Clay: {result.clay_commitment}"
        assert result.summary_lines[2] == f"Cory: {result.next_step}"


class TestCoryExactExample:
    """The canonical example from the OPS-110 correction spec."""

    def test_cory_exact_example_produces_exact_safe_output(self) -> None:
        result = _run(RecordingRunner(VALID_PAYLOAD))
        assert result.summary_lines == [
            CORY_LINE_1,
            "Clay: covering his own costs to attend Destin, bringing prepared AI materials.",
            "Cory: checking with the Destin broker owner on VIP cruise availability, "
            "following up in a day or two.",
        ]
        assert build_visible_body_lines(result) == result.summary_lines


class TestOtherContactNamesAcrossTypes:
    """Owner-label validation must work for any real first name and any
    contact_type category -- not just Cory/agent_partner."""

    @pytest.mark.parametrize(
        "contact_type,first_name,clay_phrase,contact_phrase",
        [
            (
                "borrower", "Maria",
                "locking the rate by end of week.",
                "sending over the updated pay stubs.",
            ),
            (
                "vendor", "Priya",
                "reviewing the updated service agreement.",
                "confirming pricing with her team by Monday.",
            ),
            (
                "recruit", "Jordan",
                "sending the onboarding packet today.",
                "scheduling a follow-up call for next week.",
            ),
        ],
    )
    def test_valid_labeled_lines_for_contact_type(
        self, contact_type: str, first_name: str, clay_phrase: str, contact_phrase: str
    ) -> None:
        line1 = f"A conversation about {contact_type} needs and next steps."
        payload = {
            "contact_type": contact_type,
            "summary_lines": [line1, f"Clay: {clay_phrase}", f"{first_name}: {contact_phrase}"],
            "discussed": line1,
            "clay_commitment": clay_phrase,
            "next_step": contact_phrase,
        }
        result = _run(RecordingRunner(payload), contact_context={"first_name": first_name})
        assert result.contact_type == contact_type
        assert result.summary_lines[1] == f"Clay: {clay_phrase}"
        assert result.summary_lines[2] == f"{first_name}: {contact_phrase}"

    def test_wrong_first_name_label_rejected_for_borrower(self) -> None:
        line1 = "A conversation about borrower needs and next steps."
        payload = {
            "contact_type": "borrower",
            "summary_lines": [line1, "Clay: locks the rate.", "Maria: sends pay stubs."],
            "discussed": line1,
            "clay_commitment": "locks the rate.",
            "next_step": "sends pay stubs.",
        }
        with pytest.raises(SummarizerError) as exc_info:
            _run(RecordingRunner(payload), contact_context={"first_name": "Marissa"})
        assert exc_info.value.error_class == "invalid_summary_lines"


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
            contact_context=dict(DEFAULT_CONTACT_CONTEXT),
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


def _summary_result(
    *,
    discussed: str = CORY_LINE_1,
    clay_commitment: str = CORY_CLAY_PHRASE,
    next_step: str = CORY_CONTACT_PHRASE,
    summary_lines: list[str] | None = None,
) -> SummaryResult:
    return SummaryResult(
        contact_type="agent_partner",
        summary_lines=summary_lines or [
            CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}", f"Cory: {CORY_CONTACT_PHRASE}",
        ],
        discussed=discussed,
        clay_commitment=clay_commitment,
        next_step=next_step,
    )


class TestBuildVisibleBodyLines:
    """The visible GHL note body is exactly the validated summary_lines --
    never a re-synthesis of discussed/clay_commitment/next_step."""

    def test_returns_summary_lines_unchanged(self) -> None:
        result = _summary_result()
        assert build_visible_body_lines(result) == result.summary_lines

    def test_two_line_result_returns_two_lines(self) -> None:
        result = _summary_result(
            summary_lines=[CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"],
            next_step="None stated.",
        )
        assert build_visible_body_lines(result) == [CORY_LINE_1, f"Clay: {CORY_CLAY_PHRASE}"]

    def test_defense_in_depth_returns_none_for_one_line(self) -> None:
        """_validate_and_build can never actually produce this, but
        build_visible_body_lines must still fail closed if it ever did."""
        result = _summary_result(summary_lines=["only one line."])
        assert build_visible_body_lines(result) is None

    def test_defense_in_depth_returns_none_for_four_lines(self) -> None:
        result = _summary_result(summary_lines=["a.", "b.", "c.", "d."])
        assert build_visible_body_lines(result) is None
