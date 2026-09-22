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
        f"Write strict JSON only -- no markdown fences, no prose outside the JSON "
        f"object -- to {output_path}, with exactly these keys:\n"
        '  "contact_type": one of the categories above.\n'
        '  "summary_lines": an array of 2 or 3 short, non-empty, plain-text '
        "strings (no markdown bullets, no headings, no URLs, no tracking IDs, "
        "no raw phone numbers or email addresses, no verbatim transcript "
        "quotes). This becomes the visible note body, joined with newlines.\n"
        '  "discussed": one plain sentence describing what was discussed.\n'
        '  "clay_commitment": one plain sentence describing what Clay '
        f'specifically committed to, or exactly the string "{_NONE_STATED}" if '
        "Clay made no commitment.\n"
        f'  "next_step": one plain sentence describing the next step, or exactly '
        f'the string "{_NONE_STATED}" if none was stated.\n'
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
    return False


def _validate_and_build(payload: Any) -> SummaryResult:
    if not isinstance(payload, dict):
        raise SummarizerError("invalid_json_shape")
    if not _REQUIRED_KEYS.issubset(payload.keys()):
        raise SummarizerError("missing_keys")

    contact_type = payload.get("contact_type")
    if contact_type not in CONTACT_TYPES:
        raise SummarizerError("invalid_contact_type")

    summary_lines = payload.get("summary_lines")
    if not isinstance(summary_lines, list) or len(summary_lines) not in (2, 3):
        raise SummarizerError("invalid_summary_lines")
    clean_lines: list[str] = []
    for line in summary_lines:
        if not isinstance(line, str) or _looks_unsafe(line):
            raise SummarizerError("invalid_summary_lines")
        clean_lines.append(line.strip())

    discussed = payload.get("discussed")
    if not isinstance(discussed, str) or _looks_unsafe(discussed):
        raise SummarizerError("invalid_discussed")

    clay_commitment = payload.get("clay_commitment")
    if not isinstance(clay_commitment, str) or not clay_commitment.strip():
        raise SummarizerError("invalid_clay_commitment")
    if clay_commitment.strip() != _NONE_STATED and _looks_unsafe(clay_commitment):
        raise SummarizerError("invalid_clay_commitment")

    next_step = payload.get("next_step")
    if not isinstance(next_step, str) or not next_step.strip():
        raise SummarizerError("invalid_next_step")
    if next_step.strip() != _NONE_STATED and _looks_unsafe(next_step):
        raise SummarizerError("invalid_next_step")

    return SummaryResult(
        contact_type=contact_type,
        summary_lines=clean_lines,
        discussed=discussed.strip(),
        clay_commitment=clay_commitment.strip(),
        next_step=next_step.strip(),
    )


def build_visible_body_lines(result: SummaryResult) -> list[str] | None:
    """Deterministically compose the visible note body from the structured
    discussed/clay_commitment/next_step fields -- never from the free-form
    summary_lines, which the model could write without ever mentioning
    Clay's commitment or the next step. Line order is fixed: discussed,
    then clay_commitment (unless "None stated."), then next_step (unless
    "None stated."). Returns None when that would produce fewer than 2
    lines, meaning the caller must treat this summarizer output as invalid
    and write no note rather than substituting or inline-summarizing."""
    lines = [result.discussed]
    if result.clay_commitment != _NONE_STATED:
        lines.append(result.clay_commitment)
    if result.next_step != _NONE_STATED:
        lines.append(result.next_step)
    if len(lines) < 2:
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

        return _validate_and_build(payload)
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
