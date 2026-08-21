"""Tests for the append-only write audit log primitive.

Covers the mechanics the write clients depend on: append-only file handling,
``0600`` permissions, one fsync per append, the loud failure mode, and the
structural guarantees (no non-append file modes, no code that shortens the log).
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.write_audit_log import (
    AFTER_UNKNOWN,
    KNOWN_DESTINATIONS,
    MissingWriteTriggerError,
    WriteAuditLogError,
    WriteAuditOutcomeLogError,
    WriteAuditRecorder,
    append_entry,
    default_write_audit_dir,
    format_timestamp,
    iter_entries,
    require_trigger,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every source file introduced by this feature.  The structural tests below scan
#: all of them, not just the log module, so a stray non-append open or a
#: log-shortening helper cannot hide in a client.
FEATURE_SOURCES = (
    REPO_ROOT / "tools" / "write_audit_log.py",
    REPO_ROOT / "tools" / "sync_json_http.py",
    REPO_ROOT / "tools" / "msgraph_write_client.py",
    REPO_ROOT / "tools" / "ghl_client.py",
    REPO_ROOT / "scripts" / "write_audit_query.py",
)


def fixed_clock(*moments: datetime):
    """Return a clock callable that yields *moments* in order, repeating the last."""
    remaining = list(moments)

    def _clock() -> datetime:
        return remaining[0] if len(remaining) == 1 else remaining.pop(0)

    return _clock


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ── Real-filesystem durability and permissions (criteria 9) ──────────────────


class TestRealFilesystemAppend:
    def test_creates_month_file_with_0600_permissions(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "write_audit"
        moment = datetime(2026, 8, 21, 22, 15, 3, 412000, tzinfo=timezone.utc)

        path = append_entry({"timestamp": format_timestamp(moment)}, log_dir=log_dir, moment=moment)

        assert path == log_dir / "2026-08.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_appending_to_a_populated_file_leaves_every_existing_byte_untouched(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "write_audit"
        moment = datetime(2026, 8, 21, 1, 0, 0, tzinfo=timezone.utc)

        for index in (1, 2, 3):
            path = append_entry({"n": index, "note": "seed"}, log_dir=log_dir, moment=moment)
        existing_bytes = path.read_bytes()
        existing_lines = read_lines(path)
        assert len(existing_lines) == 3

        append_entry({"n": 4, "note": "the one more write"}, log_dir=log_dir, moment=moment)

        final_bytes = path.read_bytes()
        assert final_bytes[: len(existing_bytes)] == existing_bytes
        final_lines = read_lines(path)
        assert final_lines[:3] == existing_lines
        assert len(final_lines) == 4
        assert [json.loads(line)["n"] for line in final_lines] == [1, 2, 3, 4]

    def test_every_line_parses_as_one_json_object(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "write_audit"
        moment = datetime(2026, 8, 21, 1, 0, 0, tzinfo=timezone.utc)
        for index in range(5):
            append_entry({"n": index, "unicode": "Zoë — ok"}, log_dir=log_dir, moment=moment)

        parsed = [json.loads(line) for line in read_lines(log_dir / "2026-08.jsonl")]
        assert [entry["n"] for entry in parsed] == [0, 1, 2, 3, 4]
        assert all(isinstance(entry, dict) for entry in parsed)

    def test_the_log_file_is_fsynced_exactly_once_per_append(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """One file fsync per append, plus one directory fsync when a month starts.

        The directory fsync only happens on the append that creates a month file:
        without it the line's bytes would be durable while the directory entry
        pointing at them was not.
        """
        import tools.write_audit_log as module

        calls: list[str] = []
        real_fsync = os.fsync

        def counting_fsync(fd: int) -> None:
            calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        monkeypatch.setattr(module.os, "fsync", counting_fsync)

        log_dir = tmp_path / "write_audit"
        moment = datetime(2026, 8, 21, 1, 0, 0, tzinfo=timezone.utc)
        next_month = datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc)

        append_entry({"n": 1}, log_dir=log_dir, moment=moment)
        assert calls == ["file", "dir"]

        append_entry({"n": 2}, log_dir=log_dir, moment=moment)
        assert calls == ["file", "dir", "file"]

        append_entry({"n": 3}, log_dir=log_dir, moment=next_month)
        assert calls == ["file", "dir", "file", "file", "dir"]

        assert calls.count("file") == 3  # exactly one per append

    def test_entries_land_in_the_month_file_matching_their_timestamp(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "write_audit"
        july = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        august = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

        assert append_entry({"m": 7}, log_dir=log_dir, moment=july).name == "2026-07.jsonl"
        assert append_entry({"m": 8}, log_dir=log_dir, moment=august).name == "2026-08.jsonl"


# ── The gate: a failed append blocks the write, loudly ───────────────────────


class TestGateFailsLoudly:
    def test_read_only_log_directory_raises_write_audit_log_error(
        self, tmp_path: Path
    ) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")

        log_dir = tmp_path / "readonly"
        log_dir.mkdir()
        os.chmod(log_dir, 0o500)
        try:
            recorder = WriteAuditRecorder(
                destination="msgraph_contacts", actor="msgraph_contacts_client", log_dir=log_dir
            )
            with pytest.raises(WriteAuditLogError) as excinfo:
                recorder.authorize_write(
                    operation="update", record_id="rec-1", before={"a": 1}, trigger="why"
                )
        finally:
            os.chmod(log_dir, 0o700)

        message = str(excinfo.value)
        # The message has to name the blocked destination write, the record, and why.
        assert "msgraph_contacts" in message
        assert "update" in message
        assert "rec-1" in message
        assert "NOT attempted" in message
        assert "PermissionError" in message or "Errno" in message

    def test_unserializable_payload_blocks_before_the_file_is_touched(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "write_audit"
        recorder = WriteAuditRecorder(
            destination="ghl_contacts", actor="ghl_client", log_dir=log_dir
        )

        with pytest.raises(WriteAuditLogError):
            recorder.authorize_write(
                operation="update",
                record_id="rec-1",
                before={"created": object()},
                trigger="serialization failure drill",
            )

        # No partial line was left behind.
        assert list(iter_entries(log_dir)) == []

    def test_outcome_append_failure_says_the_write_already_happened(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import tools.write_audit_log as module

        log_dir = tmp_path / "write_audit"
        recorder = WriteAuditRecorder(
            destination="msgraph_tasks", actor="msgraph_tasks_client", log_dir=log_dir
        )
        authorized = recorder.authorize_write(
            operation="create", record_id=None, before=None, trigger="task drill"
        )

        monkeypatch.setattr(
            module, "append_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )
        with pytest.raises(WriteAuditOutcomeLogError) as excinfo:
            authorized.record_outcome(record_id="task-9", after={"id": "task-9"})

        message = str(excinfo.value)
        assert "SUCCEEDED" in message
        assert "DO NOT RETRY" in message

    def test_the_two_failure_modes_are_distinguishable_without_reading_the_message(
        self,
    ) -> None:
        """A caller with a blanket retry handler needs to tell these apart.

        Retrying a blocked write is correct; retrying after an outcome-append
        failure double-writes, because the destination already applied it.
        """
        assert WriteAuditLogError.write_completed is False
        assert WriteAuditOutcomeLogError.write_completed is True


# ── Trigger is required ──────────────────────────────────────────────────────


class TestTriggerRequired:
    @pytest.mark.parametrize("bad", [None, "", "   ", 42])
    def test_blank_trigger_is_rejected(self, bad, tmp_path: Path) -> None:
        recorder = WriteAuditRecorder(
            destination="ghl_contacts", actor="ghl_client", log_dir=tmp_path / "write_audit"
        )
        with pytest.raises(MissingWriteTriggerError):
            recorder.authorize_write(
                operation="create", record_id=None, before=None, trigger=bad
            )
        assert list(iter_entries(tmp_path / "write_audit")) == []

    def test_require_trigger_strips_and_returns(self) -> None:
        assert require_trigger("  Nathan intro triage 2026-08-21 ") == "Nathan intro triage 2026-08-21"


# ── Entry shape ──────────────────────────────────────────────────────────────


class TestEntryShape:
    def test_intent_and_outcome_lines_share_an_audit_id_and_required_fields(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "write_audit"
        recorder = WriteAuditRecorder(
            destination="msgraph_contacts",
            actor="msgraph_contacts_client",
            log_dir=log_dir,
            clock=fixed_clock(datetime(2026, 8, 21, 22, 15, 3, 412000, tzinfo=timezone.utc)),
        )
        authorized = recorder.authorize_write(
            operation="update",
            record_id="rec-1",
            before={"displayName": "Old"},
            trigger="Nathan intro triage 2026-08-21",
        )
        authorized.record_outcome(after={"displayName": "New"})

        entries = [entry for _, _, entry in iter_entries(log_dir)]
        assert [entry["audit_phase"] for entry in entries] == ["intent", "outcome"]
        assert entries[0]["audit_id"] == entries[1]["audit_id"]

        for entry in entries:
            for required in (
                "timestamp",
                "destination",
                "record_id",
                "operation",
                "before",
                "after",
                "actor",
                "trigger",
            ):
                assert required in entry
            assert entry["destination"] == "msgraph_contacts"
            assert entry["actor"] == "msgraph_contacts_client"
            assert entry["trigger"] == "Nathan intro triage 2026-08-21"

        assert entries[0]["after"] is None
        assert entries[1]["after"] == {"displayName": "New"}
        assert "after_fetch_failed" not in entries[1]

    def test_after_fetch_failure_flags_the_outcome_entry(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "write_audit"
        recorder = WriteAuditRecorder(
            destination="ghl_contacts", actor="ghl_client", log_dir=log_dir
        )
        authorized = recorder.authorize_write(
            operation="create", record_id=None, before=None, trigger="drill"
        )
        authorized.record_outcome(record_id="c-1", after={"ignored": True}, after_fetch_failed=True)

        outcome = [entry for _, _, entry in iter_entries(log_dir)][-1]
        assert outcome["after"] == AFTER_UNKNOWN
        assert outcome["after"] == "UNKNOWN — verify manually"
        assert outcome["after_fetch_failed"] is True
        assert outcome["record_id"] == "c-1"

    def test_timestamp_is_iso8601_utc_with_milliseconds(self) -> None:
        moment = datetime(2026, 8, 21, 22, 15, 3, 412999, tzinfo=timezone.utc)
        rendered = format_timestamp(moment)
        assert rendered == "2026-08-21T22:15:03.412Z"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", rendered)
        # Round-trips through the stdlib parser.
        assert datetime.fromisoformat(rendered.replace("Z", "+00:00")) == moment.replace(
            microsecond=412000
        )

    @pytest.mark.parametrize("destination", KNOWN_DESTINATIONS)
    def test_all_required_destinations_are_accepted(self, destination, tmp_path: Path) -> None:
        WriteAuditRecorder(destination=destination, actor="x", log_dir=tmp_path)

    def test_unknown_destination_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown write-audit destination"):
            WriteAuditRecorder(destination="salesforce", actor="x", log_dir=tmp_path)

    def test_unknown_operation_is_rejected_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "write_audit"
        recorder = WriteAuditRecorder(
            destination="ghl_contacts", actor="ghl_client", log_dir=log_dir
        )
        with pytest.raises(ValueError, match="Unknown write operation"):
            recorder.authorize_write(
                operation="upsert", record_id=None, before=None, trigger="drill"
            )
        assert list(iter_entries(log_dir)) == []

    def test_default_log_dir_lives_under_hermes_home(self) -> None:
        assert default_write_audit_dir().parts[-2:] == ("logs", "write_audit")


# ── Structural guarantees (criteria 4 and 5) ─────────────────────────────────


class TestStructuralGuarantees:
    """These assert on the source itself, not on behaviour.

    "We don't call the function that shortens the log" is not good enough — the
    function must not exist for anyone to wire up later.
    """

    #: Names/semantics that must not appear anywhere in this feature.
    FORBIDDEN_TOKENS = ("prune", "rotate", "cleanup", "retention", "max_age", "expire")

    @pytest.mark.parametrize("source", FEATURE_SOURCES, ids=lambda p: p.name)
    def test_no_data_reducing_identifiers_exist(self, source: Path) -> None:
        text = source.read_text(encoding="utf-8")
        for token in self.FORBIDDEN_TOKENS:
            assert token not in text.lower(), (
                f"{source.name} mentions {token!r}. The write audit log keeps entries "
                f"indefinitely; no code that shortens or ages out the log may exist."
            )

    @pytest.mark.parametrize("source", FEATURE_SOURCES, ids=lambda p: p.name)
    def test_no_function_removes_or_truncates_log_files(self, source: Path) -> None:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        removal_calls = {"unlink", "remove", "rmtree", "truncate", "rename", "replace"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in removal_calls, (
                    f"{source.name} calls {node.func.attr}() — the audit log is "
                    f"append-only and nothing may take a log file away."
                )

    @pytest.mark.parametrize("source", FEATURE_SOURCES, ids=lambda p: p.name)
    def test_no_file_is_opened_for_writing_in_a_non_append_mode(self, source: Path) -> None:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "open":
                continue
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [
                kw.value.value
                for kw in node.keywords
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
            ]
            for mode in modes:
                assert mode in ("a", "r"), (
                    f"{source.name} opens a file with mode {mode!r}. The audit log may "
                    f"only ever be opened for append (and read-only for querying)."
                )

    def test_the_only_low_level_open_flags_are_append_flavoured(self) -> None:
        text = (REPO_ROOT / "tools" / "write_audit_log.py").read_text(encoding="utf-8")
        assert "os.O_APPEND" in text
        assert "os.O_TRUNC" not in text
