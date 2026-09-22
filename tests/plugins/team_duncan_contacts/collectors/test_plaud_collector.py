"""Tests for the Plaud MCP metadata collector.

Uses only an in-memory fake transport. No live MCP connection, no real
credentials, no transcript/summary/mind-map content anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plugins.team_duncan_contacts.collectors.plaud_collector import (
    PlaudCollector,
    normalize_record,
)


class FakePlaudTransport:
    def __init__(self, boundary: str, records: list[dict]) -> None:
        self._boundary = boundary
        self._records = records
        self.fetch_since_calls: list[str | None] = []
        self.identity_lookups: list[str] = []

    def get_deployment_boundary(self) -> str:
        return self._boundary

    def fetch_records_since(self, checkpoint):
        self.fetch_since_calls.append(checkpoint)
        return [r for r in self._records if checkpoint is None or r["recording_id"] > checkpoint]

    def fetch_record_by_identity(self, recording_id: str):
        self.identity_lookups.append(recording_id)
        for r in self._records:
            if r["recording_id"] == recording_id:
                return r
        return None


def _record(recording_id="rec-1", **overrides) -> dict:
    base = {
        "recording_id": recording_id,
        "start_time": "2026-01-01T12:00:00+00:00",
        "duration_s": 90,
        "caller_handle": "+15551234567",
        "transcript_available": True,
        "summary_available": False,
    }
    base.update(overrides)
    return base


def test_normalize_record_happy_path() -> None:
    rec = normalize_record(_record())
    assert rec.source == "plaud"
    assert rec.source_event_id == "rec-1"
    assert rec.duration_s == 90
    assert rec.caller_handle == "+15551234567"
    assert rec.transcript_available is True
    assert rec.summary_available is False


def test_normalize_record_rejects_mind_map() -> None:
    with pytest.raises(ValueError):
        normalize_record(_record(mind_map={"nodes": []}))


def test_normalize_record_requires_identity_and_start_time() -> None:
    with pytest.raises(ValueError):
        normalize_record(_record(recording_id=""))
    with pytest.raises(ValueError):
        normalize_record({"recording_id": "x", "caller_handle": "+1"})


def test_normalize_record_allows_missing_caller_handle() -> None:
    """Plaud's real `list_files` metadata contract carries no caller-handle
    field at all -- normalize_record must not require one."""
    raw = _record()
    del raw["caller_handle"]
    rec = normalize_record(raw)
    assert rec.caller_handle is None
    assert rec.source_event_id == "rec-1"


def test_availability_flag_accepts_bool_and_int_length() -> None:
    rec = normalize_record(_record(transcript_available=0, summary_available=120))
    assert rec.transcript_available is False
    assert rec.summary_available is True


def test_availability_flag_unknown_shape_is_none() -> None:
    rec = normalize_record(_record(transcript_available="yes"))
    assert rec.transcript_available is None


def test_provenance_never_carries_raw_handle_or_content() -> None:
    rec = normalize_record(_record())
    prov = rec.provenance()
    assert set(prov.keys()) == {
        "plaud_recording_id", "duration_s", "transcript_available", "summary_available",
    }
    assert "+15551234567" not in str(prov.values())


def test_deployment_boundary_cursor_no_backfill() -> None:
    transport = FakePlaudTransport("rec-100", [_record("rec-050"), _record("rec-150")])
    collector = PlaudCollector(transport)
    boundary = collector.initialize_cursor()
    assert boundary == "rec-100"
    records = collector.fetch_new(boundary)
    # Only records at/after the boundary -- rec-050 (before) is excluded.
    assert [r.source_event_id for r in records] == ["rec-150"]


def test_fetch_by_identity_sealed_refetch() -> None:
    transport = FakePlaudTransport("rec-000", [_record("rec-1")])
    collector = PlaudCollector(transport)
    found = collector.fetch_by_identity("rec-1")
    assert found is not None
    assert found.source_event_id == "rec-1"

    missing = collector.fetch_by_identity("does-not-exist")
    assert missing is None
