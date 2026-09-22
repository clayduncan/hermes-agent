"""OPS-110 Claude Code call summarizer.

Every call summary is authored by invoking `~/.hermes/bin/claude-max` as a
one-shot subprocess -- never inline LLM logic inside this repo's own agent
loop, and never Plaud's own summary/outline. The transcript and the bounded
GHL contact context are written to a fresh, mode-0700 temporary directory
outside this repo (under the OS temp root), each file mode 0600; Claude Code
is instructed, via a fixed prompt, to read exactly those two files and write
its answer as strict JSON to a third fixed path in that same directory --
its only permitted write. Every input/output file is removed in `finally`,
success or failure, so no transcript or summary content is ever left on
disk after this function returns.

On any failure -- nonzero exit, timeout, missing/oversized/unparseable
output, or a schema violation -- this module raises SummarizerError with a
fixed, content-free `error_class`. The caller must not write a GHL note and
must not persist anything beyond that error_class.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .sanitizer import contains_phone_like

CONTACT_TYPES: tuple[str, ...] = (
    "borrower", "agent_partner", "caravan", "recruit", "vendor", "other",
)

#: Fixed subprocess timeout, in seconds. Not configurable.
SUMMARIZER_TIMEOUT_S = 180

#: Fixed maximum output.json size read from disk. Not configurable.
MAX_OUTPUT_BYTES = 65_536

#: Fixed path to the claude-max binary, relative to HERMES_HOME.
CLAUDE_MAX_RELATIVE_PATH = "bin/claude-max"

_TRANSCRIPT_FILENAME = "transcript.json"
_CONTEXT_FILENAME = "context.json"
_OUTPUT_FILENAME = "output.json"

_NONE_STATED = "None stated."

_REQUIRED_KEYS = frozenset(
    {"contact_type", "summary_lines", "discussed", "clay_commitment", "next_step"}
)

#: The fixed speaker label for Clay's own commitment/next-step line. Never
#: configurable -- Clay is always Clay in this CRM.
_CLAY_LABEL = "Clay: "

#: Whole-word first-person pronouns. Every visible line and every durable
#: field must be strictly third person.
_FIRST_PERSON_RE = re.compile(r"\b(i|me|my|mine|we|us|our|ours)\b", re.IGNORECASE)

#: A simple email pattern, defense-in-depth alongside contains_phone_like --
#: neither a raw phone number nor a raw email address may ever reach the
#: visible note or a durable field.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_EM_DASH = "—"

#: Narration verbs that make a lead-in a summary of the call rather than the
#: substance of it -- e.g. "Clay and Cory discussed", "Cory talked".
_NARRATION_VERBS = (
    "discussed", "talked", "spoke", "chatted", "caught up", "touched base",
    "met to discuss",
)

_NORMALIZE_STRIP_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_for_containment(text: str) -> str:
    lowered = text.strip().lower()
    stripped = _NORMALIZE_STRIP_RE.sub("", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _mutually_contains(a: str, b: str) -> bool:
    na, nb = _normalize_for_containment(a), _normalize_for_containment(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def _has_narrative_leadin(line: str, contact_first_name: str) -> bool:
    stripped = line.strip()
    if re.match(r"^discussed\b", stripped, re.IGNORECASE):
        return True
    names = [re.escape("clay"), re.escape(contact_first_name.lower())]
    name_alt = "|".join(names)
    verb_alt = "|".join(re.escape(v) for v in _NARRATION_VERBS)
    pattern = re.compile(
        rf"^(?:{name_alt})(?:\s+(?:and|&)\s+(?:{name_alt}))?\s+(?:{verb_alt})\b",
        re.IGNORECASE,
    )
    return bool(pattern.match(stripped))


class SummarizerError(RuntimeError):
    """Raised for every claude-max summarizer failure path. `error_class` is
    always a fixed classification string -- never subprocess stdout/stderr,
    transcript content, or model output text."""

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(error_class)


@dataclass
class SummaryResult:
    contact_type: str
    summary_lines: list[str]
    discussed: str
    clay_commitment: str
    next_step: str


def _fixed_prompt(transcript_path: Path, context_path: Path, output_path: Path) -> str:
    types = ", ".join(CONTACT_TYPES)
    return (
        "You are authoring a short, factual call-summary note for a real-estate "
        "CRM contact record.\n\n"
        f"Read the call transcript at {transcript_path} (a JSON array of "
        "{\"speaker\", \"text\"} segments) and the contact context at "
        f"{context_path} (a bounded JSON object: contact_id, first_name, "
        "last_name, type, tags, company_name, email_domain, custom_fields).\n\n"
        "Direct-tool policy: do not use the Cua Driver or any computer_use tool. "
        f"Do not write, create, or modify any file other than {output_path}. "
        "Do not access the network or any live system. Do not change any skill, "
        "memory, or configuration file. Do not run any command beyond reading "
        "the two input files above.\n\n"
        "Classify the relationship from the transcript and contact context as "
        f"exactly one of: {types}. Do not force a mortgage/borrower framing when "
        "the conversation is clearly a different relationship (agent partner, "
        "Caravan participant, recruit, vendor, or other).\n\n"
        "Carefully distinguish who said or committed to what. Never attribute one "
        "participant's commitment or statement to a different participant.\n\n"
        "The visible note has exactly two possible speakers: Clay, and the "
        "contact named by the first_name field in context.json (use that exact "
        "first name -- never a nickname, last name, or full name). Write "
        "strictly third person throughout every field: never first person -- no "
        "I, me, my, mine, we, us, our, ours. Never use an em dash.\n\n"
        f"Write strict JSON only -- no markdown fences, no prose outside the JSON "
        f"object -- to {output_path}, with exactly these keys:\n"
        '  "contact_type": one of the categories above.\n'
        '  "summary_lines": an array of exactly 2 or 3 short, plain-text '
        "strings forming the entire visible note body, in this exact shape:\n"
        "    - Line 1 opens directly with the substance of the call -- never a "
        'narration lead-in such as "Clay and Cory discussed", "Clay discussed", '
        '"Cory and Clay talked", or "Discussed". It must not start with a '
        "speaker label.\n"
        "    - Each remaining line is exactly one speaker's commitment or next "
        'step and must start with exactly "Clay: " or exactly "<the contact\'s '
        'first_name>: " -- no other label, no nickname, no last name. At most '
        "one line per speaker -- never two lines for the same speaker. Include "
        "a Clay line only if Clay stated a commitment; include a contact line "
        "only if the contact stated a next step. That means exactly 2 lines "
        "total when only one speaker has one, and exactly 3 lines when both "
        "do.\n"
        "    - No markdown bullets or headings, no URLs, no tracking IDs, no "
        "raw phone numbers or email addresses, no verbatim transcript quotes, "
        "no em dashes, on any line.\n"
        '  "discussed": one plain, third-person sentence describing what was '
        "discussed. This is a durable record only -- it is not shown verbatim "
        "on the note.\n"
        '  "clay_commitment": exactly the action-phrase text that appears after '
        '"Clay: " on its summary line, with that label itself omitted here, or '
        f'exactly the string "{_NONE_STATED}" if Clay made no commitment (and '
        "there is then no Clay line in summary_lines).\n"
        '  "next_step": exactly the action-phrase text that appears after the '
        'contact\'s label on its summary line, with that label itself omitted '
        f'here, or exactly the string "{_NONE_STATED}" if the contact stated no '
        "next step (and there is then no contact line in summary_lines).\n"
    )


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    """Default production runner: one-shot `argv` execution, never a shell."""
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=SUMMARIZER_TIMEOUT_S,
        check=False,
    )


def _write_0600(path: Path, payload: Any) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _looks_unsafe(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped[0] in "-*#":
        return True
    if "http://" in text or "https://" in text:
        return True
    if contains_phone_like(text):
        return True
    if _EMAIL_RE.search(text):
        return True
    if _EM_DASH in text:
        return True
    return False


def _validate_summary_lines(
    summary_lines: Any, *, contact_first_name: str
) -> tuple[list[str], str | None, str | None]:
    """Strictly validate the visible-note-shaped summary_lines array.
    Returns (clean_lines, clay_action_phrase_or_None,
    contact_action_phrase_or_None), where the action phrases are each
    line's content with its speaker label stripped -- used by the caller to
    cross-check against the durable clay_commitment/next_step fields."""
    if not isinstance(summary_lines, list) or len(summary_lines) not in (2, 3):
        raise SummarizerError("invalid_summary_lines")

    clean_lines: list[str] = []
    for line in summary_lines:
        if not isinstance(line, str) or _looks_unsafe(line):
            raise SummarizerError("invalid_summary_lines")
        clean_lines.append(line.strip())

    contact_label = f"{contact_first_name}: "

    line1 = clean_lines[0]
    if line1.startswith(_CLAY_LABEL) or line1.startswith(contact_label):
        raise SummarizerError("invalid_summary_lines")
    if _has_narrative_leadin(line1, contact_first_name):
        raise SummarizerError("invalid_summary_lines")
    if _FIRST_PERSON_RE.search(line1):
        raise SummarizerError("invalid_summary_lines")

    seen_owners: set[str] = set()
    clay_phrase: str | None = None
    contact_phrase: str | None = None
    for line in clean_lines[1:]:
        if line.startswith(_CLAY_LABEL):
            owner = "clay"
            phrase = line[len(_CLAY_LABEL):].strip()
        elif line.startswith(contact_label):
            owner = "contact"
            phrase = line[len(contact_label):].strip()
        else:
            # Unlabeled, or labeled with a name other than Clay/the contact.
            raise SummarizerError("invalid_summary_lines")

        if owner in seen_owners:
            raise SummarizerError("invalid_summary_lines")
        seen_owners.add(owner)

        if not phrase or _FIRST_PERSON_RE.search(phrase):
            raise SummarizerError("invalid_summary_lines")

        if owner == "clay":
            clay_phrase = phrase
        else:
            contact_phrase = phrase

    return clean_lines, clay_phrase, contact_phrase


def _validate_and_build(payload: Any, *, contact_first_name: str) -> SummaryResult:
    if not isinstance(payload, dict):
        raise SummarizerError("invalid_json_shape")
    if not _REQUIRED_KEYS.issubset(payload.keys()):
        raise SummarizerError("missing_keys")

    contact_type = payload.get("contact_type")
    if contact_type not in CONTACT_TYPES:
        raise SummarizerError("invalid_contact_type")

    discussed = payload.get("discussed")
    if not isinstance(discussed, str) or _looks_unsafe(discussed):
        raise SummarizerError("invalid_discussed")
    if _FIRST_PERSON_RE.search(discussed):
        raise SummarizerError("invalid_discussed")
    discussed = discussed.strip()

    clay_commitment = payload.get("clay_commitment")
    if not isinstance(clay_commitment, str) or not clay_commitment.strip():
        raise SummarizerError("invalid_clay_commitment")
    clay_commitment = clay_commitment.strip()
    if clay_commitment != _NONE_STATED:
        if _looks_unsafe(clay_commitment) or _FIRST_PERSON_RE.search(clay_commitment):
            raise SummarizerError("invalid_clay_commitment")

    next_step = payload.get("next_step")
    if not isinstance(next_step, str) or not next_step.strip():
        raise SummarizerError("invalid_next_step")
    next_step = next_step.strip()
    if next_step != _NONE_STATED:
        if _looks_unsafe(next_step) or _FIRST_PERSON_RE.search(next_step):
            raise SummarizerError("invalid_next_step")

    clean_lines, clay_phrase, contact_phrase = _validate_summary_lines(
        payload.get("summary_lines"), contact_first_name=contact_first_name
    )

    clay_has_commitment = clay_commitment != _NONE_STATED
    if clay_has_commitment != (clay_phrase is not None):
        raise SummarizerError("commitment_attribution_mismatch")
    if clay_has_commitment and not _mutually_contains(clay_commitment, clay_phrase):
        raise SummarizerError("commitment_attribution_mismatch")

    contact_has_next_step = next_step != _NONE_STATED
    if contact_has_next_step != (contact_phrase is not None):
        raise SummarizerError("commitment_attribution_mismatch")
    if contact_has_next_step and not _mutually_contains(next_step, contact_phrase):
        raise SummarizerError("commitment_attribution_mismatch")

    return SummaryResult(
        contact_type=contact_type,
        summary_lines=clean_lines,
        discussed=discussed,
        clay_commitment=clay_commitment,
        next_step=next_step,
    )


def build_visible_body_lines(result: SummaryResult) -> list[str] | None:
    """The visible GHL note body is exactly Claude's validated,
    owner-labeled summary_lines -- never a re-synthesis of
    discussed/clay_commitment/next_step. Those structured fields are kept
    only as a durable record and as the cross-check _validate_and_build
    already ran to guarantee summary_lines can neither omit nor contradict
    Clay's commitment or the contact's next step. Returns None (defense in
    depth) if summary_lines does not have 2 or 3 lines, even though
    _validate_and_build already guarantees this before a SummaryResult can
    exist -- the caller must treat that as invalid summarizer output and
    write no note rather than substituting or inline-summarizing."""
    lines = list(result.summary_lines)
    if len(lines) not in (2, 3):
        return None
    return lines


def run_claude_summary(
    *,
    transcript_segments: list[dict[str, Any]],
    contact_context: dict[str, Any],
    hermes_home: Path,
    runner: Callable[[list[str]], subprocess.CompletedProcess] = _default_runner,
) -> SummaryResult:
    """Write the bounded inputs to a fresh mode-0700 temp dir, invoke
    claude-max with the fixed prompt, parse and validate its strict-JSON
    output, then remove every file in that dir. Raises SummarizerError on
    any failure -- the caller must write no GHL note in that case."""
    contact_first_name = contact_context.get("first_name")
    if not isinstance(contact_first_name, str) or not contact_first_name.strip():
        raise SummarizerError("missing_contact_first_name")
    contact_first_name = contact_first_name.strip()

    tmpdir = Path(tempfile.mkdtemp(prefix="ops110-plaud-"))
    try:
        os.chmod(tmpdir, stat.S_IRWXU)
        resolved_tmpdir = tmpdir.resolve()
        transcript_path = tmpdir / _TRANSCRIPT_FILENAME
        context_path = tmpdir / _CONTEXT_FILENAME
        output_path = tmpdir / _OUTPUT_FILENAME

        for path in (transcript_path, context_path, output_path):
            if path.resolve().parent != resolved_tmpdir:
                raise SummarizerError("unsafe_temp_path")

        _write_0600(transcript_path, transcript_segments)
        _write_0600(context_path, contact_context)

        prompt = _fixed_prompt(transcript_path, context_path, output_path)
        claude_max_bin = str(Path(hermes_home) / CLAUDE_MAX_RELATIVE_PATH)
        argv = [
            claude_max_bin,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--allowedTools",
            "Read,Write",
        ]

        try:
            completed = runner(argv)
        except subprocess.TimeoutExpired:
            raise SummarizerError("timeout") from None
        except Exception:
            raise SummarizerError("subprocess_failed_to_execute") from None

        if completed.returncode != 0:
            raise SummarizerError("nonzero_exit")

        if not output_path.exists():
            raise SummarizerError("no_output_file")

        size = output_path.stat().st_size
        if size == 0:
            raise SummarizerError("empty_output_file")
        if size > MAX_OUTPUT_BYTES:
            raise SummarizerError("output_too_large")

        try:
            raw = output_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError):
            raise SummarizerError("invalid_json") from None

        return _validate_and_build(payload, contact_first_name=contact_first_name)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


__all__ = [
    "CONTACT_TYPES",
    "SUMMARIZER_TIMEOUT_S",
    "MAX_OUTPUT_BYTES",
    "CLAUDE_MAX_RELATIVE_PATH",
    "SummarizerError",
    "SummaryResult",
    "run_claude_summary",
    "build_visible_body_lines",
]
