"""Integration tests for the OPS-110 PlaudSummaryRunner.

Uses real ContactRegistry/ActivityLedger/IngestionStateDb against temp
directories (matching test_ingestion_runner.py's precedent), plus fake
Plaud metadata/transcript transports, a fake Desk transport, a fake GHL
notes client, and a fake claude-max summarizer. No live network, no real
credentials, no live Plaud/Desk/GHL access.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.claude_summarizer import SummarizerError, SummaryResult
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    CallHistoryCollector,
    compute_desk_source_event_id,
    utc_to_apple_epoch,
)
from plugins.team_duncan_contacts.collectors.plaud_collector import PlaudCollector
from plugins.team_duncan_contacts.collectors.plaud_transcript import TranscriptPage, TranscriptSegment
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import CALL_NOTE_COLOR, NoteMirror
from plugins.team_duncan_contacts.plaud_note_writer import PlaudSummaryNoteWriter, desk_event_id
from plugins.team_duncan_contacts.plaud_summary_runner import PlaudSummaryRunner
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

IDENTITY_KEY = b"\x0b" * 32

CANARY_PHONE = "+15556665555"
CANARY_EMAIL = "cory.vasquez@realtyonegroup.com"
CANARY_TRANSCRIPT_TEXT = "this exact sentence must never leak into durable state"

CORY_RECORDING_ID = "of_43c744fd9e22636f06b4508908b73531"
CORY_CONTACT_ID = "AMYTT4eio6AxChD2UQrc"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


class FakePlaudTransport:
    def __init__(self, records=None, boundary="boundary-0"):
        self._records = records or []
        self._boundary = boundary

    def get_deployment_boundary(self):
        return self._boundary

    def fetch_records_since(self, checkpoint):
        return [r for r in self._records if checkpoint is None or r["recording_id"] > checkpoint]

    def fetch_record_by_identity(self, recording_id):
        for r in self._records:
            if r["recording_id"] == recording_id:
                return r
        return None


class FakeDeskTransport:
    def __init__(self, routine_rows=None, replay_rows=None):
        self._routine_rows = routine_rows or []
        self._replay_rows = replay_rows if replay_rows is not None else self._routine_rows

    def run_routine_scan(self, now):
        return list(self._routine_rows)

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        return list(self._replay_rows)


class FakeTranscriptTransport:
    def __init__(self, pages_by_recording: dict[str, TranscriptPage] | None = None) -> None:
        self._pages = pages_by_recording or {}
        self.fetch_calls: list[str] = []

    def fetch_transcript_page(self, recording_id: str, cursor: str | None) -> TranscriptPage:
        self.fetch_calls.append(recording_id)
        return self._pages.get(
            recording_id,
            TranscriptPage(segments=[TranscriptSegment(speaker="A", text="hello")], next_cursor=None),
        )


class FakeNoteGhlClient:
    def __init__(self, fail_create_for: set | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.update_calls = 0
        self._fail_create_for = fail_create_for or set()

    def create_note(self, contact_id, body, *, trigger, color=None, pinned=False, title=None):
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {
            "id": f"note-{self.create_calls}", "body": body, "contactId": contact_id,
            "color": color, "pinned": pinned, "title": title,
        }
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def update_note(self, contact_id, note_id, body, *, trigger, color=None, pinned=None, userId=None, title=None):
        self.update_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                n["body"] = body
                n["color"] = color
                n["title"] = title
                return dict(n)
        raise AssertionError("update_note called for a note that doesn't exist")

    def get_note(self, contact_id, note_id):
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


def _default_summary_result() -> SummaryResult:
    return SummaryResult(
        contact_type="agent_partner",
        summary_lines=[
            "Clay and Cory discussed a new listing referral.",
            "Cory will send the seller's contact information this week.",
        ],
        discussed="A referral opportunity for a new listing.",
        clay_commitment="Clay will send the standard co-listing agreement.",
        next_step="Cory will review the agreement and reply by Friday.",
    )


class FakeSummarizer:
    def __init__(self, result: SummaryResult | None = None, fail_times: int = 0) -> None:
        self.result = result or _default_summary_result()
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def __call__(self, *, transcript_segments, contact_context, hermes_home):
        self.calls.append(
            {"transcript_segments": transcript_segments, "contact_context": contact_context}
        )
        if len(self.calls) <= self.fail_times:
            raise SummarizerError("nonzero_exit")
        return self.result


def _desk_row(zdate, zaddress, zduration=1306, zoriginated=0, zanswered=1):
    return {"ZDATE": zdate, "ZADDRESS": zaddress, "ZDURATION": zduration,
            "ZORIGINATED": zoriginated, "ZANSWERED": zanswered}


def _plaud_record(recording_id, caller_handle="unused-for-identity",
                   start_time="2026-09-17T19:22:59+00:00", duration_s=1305):
    return {
        "recording_id": recording_id, "start_time": start_time, "duration_s": duration_s,
        "caller_handle": caller_handle, "transcript_available": True, "summary_available": False,
    }


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 9, 17, 20, 0, 0, tzinfo=timezone.utc))


@pytest.fixture()
def registry(tmp_path: Path, clock: _Clock) -> ContactRegistry:
    return ContactRegistry(
        tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock
    )


@pytest.fixture()
def activity_ledger(tmp_path: Path, registry: ContactRegistry) -> ActivityLedger:
    return ActivityLedger(tmp_path / "activity.db", registry)


@pytest.fixture()
def state_db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


def _activate(registry: ContactRegistry, phone: str, contact_id: str, *,
              first="Cory", last="Vasquez", at: datetime | None = None) -> str:
    reader = FakeGhlReader([{
        "id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": first, "lastName": last, "phone": phone,
    }])
    prep = registry.prepare_activation(contact_id, reader)
    assert prep.status == "ready_for_confirmation", prep.message
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"
    return confirm.contact_id


def _make_runner(
    registry, activity_ledger, state_db, clock, tmp_path, *,
    plaud_records=None, desk_rows=None, ghl_reader_contacts=None,
    summarizer=None, transcript_transport=None,
):
    plaud_collector = PlaudCollector(FakePlaudTransport(plaud_records or []))
    desk_collector = CallHistoryCollector(FakeDeskTransport(desk_rows or []), IDENTITY_KEY)
    ghl_reader = FakeGhlReader(ghl_reader_contacts or [])
    ghl_client = FakeNoteGhlClient()
    note_writer = PlaudSummaryNoteWriter(ghl_client, state_db)
    transcript = transcript_transport or FakeTranscriptTransport()
    runner = PlaudSummaryRunner(
        registry=registry,
        activity_ledger=activity_ledger,
        state_db=state_db,
        plaud_collector=plaud_collector,
        transcript_transport=transcript,
        desk_collector=desk_collector,
        ghl_reader=ghl_reader,
        note_writer=note_writer,
        hermes_home=tmp_path,
        summarizer=summarizer or FakeSummarizer(),
        clock=clock,
    )
    return runner, ghl_client, transcript


class TestUniqueMatchHappyPath:
    def test_matched_admitted_call_produces_exactly_one_note(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
                          "firstName": "Cory", "lastName": "Vasquez", "type": "other",
                          "email": CANARY_EMAIL, "phone": CANARY_PHONE}]

        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
        )
        summary = runner.run()

        assert summary.matched == 1
        assert summary.notes_written == 1
        assert summary.errors == 0
        assert ghl_client.create_calls == 1
        note = ghl_client.notes[contact_id][0]
        default_result = _default_summary_result()
        assert note["body"] == "\n".join(
            [default_result.discussed, default_result.clay_commitment, default_result.next_step]
        )
        assert note["color"] == CALL_NOTE_COLOR
        assert note["title"] == "Incoming call · Answered · 21 min 46 sec"


class TestZeroAndMultipleCandidatesFailClosed:
    def test_zero_candidates_is_unmatched_and_writes_nothing(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        plaud_records = [_plaud_record("rec-1")]
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=[],
        )
        summary = runner.run()

        assert summary.unmatched == 1
        assert summary.matched == 0
        assert ghl_client.create_calls == 0
        assert transcript.fetch_calls == []
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.match_status == "unmatched"
        assert row.desk_source_event_id is None
        assert row.contact_id is None

    def test_multiple_candidates_is_ambiguous_and_writes_nothing(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc))
        desk_rows = [
            _desk_row(zdate, CANARY_PHONE, zduration=1306),
            _desk_row(zdate + 1, CANARY_PHONE, zduration=1306),
        ]
        plaud_records = [_plaud_record("rec-1")]
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows,
        )
        summary = runner.run()

        assert summary.ambiguous == 1
        assert ghl_client.create_calls == 0
        assert transcript.fetch_calls == []
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.match_status == "ambiguous"
        assert row.desk_source_event_id is None


class TestTranscriptNeverFetchedBeforeGate:
    def test_non_activated_contact_never_reaches_transcript_fetch(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        """No activated contact anywhere in the registry: the identity gate
        must deny before any transcript fetch or note write is attempted."""
        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1")]
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows,
        )
        summary = runner.run()

        assert summary.skipped_not_activated == 1
        assert summary.notes_written == 0
        assert ghl_client.create_calls == 0
        assert transcript.fetch_calls == [], "transcript must never be fetched before the gate admits"
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.match_status == "matched"
        assert row.transcript_status == "not_fetched"

    def test_pre_activation_call_without_a_grant_is_skipped_not_queued(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        """A registered-but-not-yet-activated contact (event before the
        activation cutoff): deny_pre_activation with no override grant must
        still be skipped, never queued for review -- OPS-110 has no review
        workflow of its own."""
        contact_id = "c-1"
        event_start = datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc)
        clock.now = event_start + timedelta(hours=1)
        _activate(registry, CANARY_PHONE, contact_id)  # activated AFTER event_start
        clock.now = event_start  # rewind so the event predates activation

        zdate = utc_to_apple_epoch(event_start)
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1")]
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows,
        )
        summary = runner.run()

        assert summary.skipped_not_activated == 1
        assert transcript.fetch_calls == []
        assert ghl_client.create_calls == 0


class TestExistingEventGrantIsHonored:
    def test_override_admitted_call_proceeds_to_a_note(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        event_start = datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc)
        clock.now = event_start + timedelta(hours=1)
        _activate(registry, CANARY_PHONE, contact_id)
        clock.now = event_start

        zdate = utc_to_apple_epoch(event_start)
        desk_source_event_id = compute_desk_source_event_id(IDENTITY_KEY, zdate, CANARY_PHONE, 1306)
        activity_ledger.grant_override(
            contact_id, TEAM_DUNCAN_LOCATION_ID, "desk_call", desk_source_event_id, "manual grant for test"
        )

        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
                          "firstName": "Cory", "lastName": "Vasquez"}]
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
        )
        summary = runner.run()

        assert summary.notes_written == 1
        assert ghl_client.create_calls == 1


class TestExistingNoteUpdatedNotDuplicated:
    def test_a_note_already_mirrored_by_desk_only_ingestion_is_updated(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_source_event_id = compute_desk_source_event_id(IDENTITY_KEY, zdate, CANARY_PHONE, 1306)
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
                          "firstName": "Cory", "lastName": "Vasquez"}]

        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
        )

        # Pre-seed the exact note the Desk-only pipeline would already have created.
        desk_mirror = NoteMirror(ghl_client, state_db)
        pre_existing = desk_mirror.mirror_event(
            event_id=desk_event_id(desk_source_event_id), contact_id=contact_id,
            occurred_at=datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc),
            direction="inbound", answered=1, duration_s=1306, trigger="desk-only-mirror",
        )
        assert pre_existing.outcome == "created"
        assert ghl_client.create_calls == 1

        summary = runner.run()

        assert summary.notes_written == 1
        assert ghl_client.create_calls == 1, "must update the existing note, never create a second"
        assert ghl_client.update_calls == 1
        note = ghl_client.notes[contact_id][0]
        assert note["id"] == pre_existing.note_id
        default_result = _default_summary_result()
        assert note["body"] == "\n".join(
            [default_result.discussed, default_result.clay_commitment, default_result.next_step]
        )


class TestIdempotentReplayAndCrashRecovery:
    def test_replaying_a_completed_run_does_not_call_the_summarizer_again(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID}]
        summarizer = FakeSummarizer()

        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
            summarizer=summarizer,
        )
        first = runner.run()
        assert first.notes_written == 1
        assert len(summarizer.calls) == 1

        second = runner.run()
        assert second.notes_written == 0
        assert second.skipped_already_processed == 1
        assert len(summarizer.calls) == 1, "must not re-invoke the summarizer for an already-complete recording"
        assert ghl_client.create_calls == 1

    def test_summarizer_failure_holds_the_frontier_then_retry_succeeds(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID}]
        summarizer = FakeSummarizer(fail_times=1)

        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
            summarizer=summarizer,
        )

        first = runner.run()
        assert first.errors == 1
        assert first.notes_written == 0
        assert ghl_client.create_calls == 0
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.summary_status == "failed"
        assert row.error_class == "nonzero_exit"
        assert row.note_id is None

        second = runner.run()
        assert second.notes_written == 1
        assert ghl_client.create_calls == 1
        row2 = state_db.get_plaud_summary_state("rec-1")
        assert row2.summary_status == "complete"
        assert row2.error_class is None
        assert row2.note_id is not None


class TestNoPiiInDurableState:
    def test_durable_state_row_carries_no_raw_handle_transcript_or_summary_text(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 19, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID, "email": CANARY_EMAIL}]
        transcript = FakeTranscriptTransport(
            {"rec-1": TranscriptPage(
                segments=[TranscriptSegment(speaker="Cory", text=CANARY_TRANSCRIPT_TEXT)],
                next_cursor=None,
            )}
        )

        runner, ghl_client, _ = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
            transcript_transport=transcript,
        )
        runner.run()

        conn = sqlite3.connect(state_db._db_path)
        rows = conn.execute("SELECT * FROM plaud_summary_state").fetchall()
        note_mirror_rows = conn.execute("SELECT * FROM note_mirror").fetchall()
        blob = str(rows) + str(note_mirror_rows)
        conn.close()

        assert CANARY_PHONE not in blob
        assert CANARY_EMAIL not in blob
        assert CANARY_TRANSCRIPT_TEXT not in blob


class TestCoryRealFixtureMatch:
    def test_cory_fixture_metadata_uniquely_matches_the_expected_desk_call(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        """The ticket's verified real seam: Plaud recording
        of_43c744fd9e22636f06b4508908b73531 (start 19:22:59, duration
        1,305,000 ms) against the Desk call for Cory Vasquez (inbound,
        answered, start 19:24:30.029607Z, duration ~1306.196s), resolving
        to GHL contact AMYTT4eio6AxChD2UQrc -- using metadata correlation
        and the activation gate only, never transcript/title identity.
        """
        clock.now = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
        _activate(registry, CANARY_PHONE, CORY_CONTACT_ID, first="Cory", last="Vasquez")
        clock.now = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)

        desk_start = datetime(2026, 9, 17, 19, 24, 30, 29607, tzinfo=timezone.utc)
        zdate = utc_to_apple_epoch(desk_start)
        desk_source_event_id = compute_desk_source_event_id(IDENTITY_KEY, zdate, CANARY_PHONE, 1306)
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306, zoriginated=0, zanswered=1)]

        plaud_records = [
            _plaud_record(
                CORY_RECORDING_ID, start_time="2026-09-17T19:22:59+00:00", duration_s=1305,
            )
        ]
        ghl_contacts = [{
            "id": CORY_CONTACT_ID, "locationId": TEAM_DUNCAN_LOCATION_ID,
            "firstName": "Cory", "lastName": "Vasquez", "type": "other", "email": CANARY_EMAIL,
        }]

        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
        )
        summary = runner.run()

        assert summary.matched == 1
        assert summary.notes_written == 1
        row = state_db.get_plaud_summary_state(CORY_RECORDING_ID)
        assert row.desk_source_event_id == desk_source_event_id
        assert row.contact_id == CORY_CONTACT_ID
        assert row.note_id is not None
        assert ghl_client.notes[CORY_CONTACT_ID][0]["title"] == "Incoming call · Answered · 21 min 46 sec"


class TestVisibleBodyIsStructuredNotFreeForm:
    """The visible note body must be deterministically composed from
    Claude's structured discussed/clay_commitment/next_step fields, never
    from the free-form summary_lines -- proving the OPS-110 real-proof
    correction: Clay's commitment and the next step can no longer be
    silently dropped from the note."""

    def _setup(self, registry, activity_ledger, state_db, clock, tmp_path, *, summary_result):
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)

        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
                          "firstName": "Cory", "lastName": "Vasquez"}]
        summarizer = FakeSummarizer(result=summary_result)
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
            summarizer=summarizer,
        )
        return contact_id, runner, ghl_client

    def test_all_three_fields_produce_a_three_line_body(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        result = _default_summary_result()
        contact_id, runner, ghl_client = self._setup(
            registry, activity_ledger, state_db, clock, tmp_path, summary_result=result,
        )
        summary = runner.run()
        assert summary.notes_written == 1
        note = ghl_client.notes[contact_id][0]
        assert note["body"] == f"{result.discussed}\n{result.clay_commitment}\n{result.next_step}"

    def test_no_clay_commitment_produces_a_two_line_body(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        result = SummaryResult(
            contact_type="agent_partner",
            summary_lines=["Line one.", "Line two."],
            discussed="A referral opportunity for a new listing.",
            clay_commitment="None stated.",
            next_step="Cory will review the agreement and reply by Friday.",
        )
        contact_id, runner, ghl_client = self._setup(
            registry, activity_ledger, state_db, clock, tmp_path, summary_result=result,
        )
        summary = runner.run()
        assert summary.notes_written == 1
        note = ghl_client.notes[contact_id][0]
        assert note["body"] == f"{result.discussed}\n{result.next_step}"

    def test_no_next_step_produces_a_two_line_body(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        result = SummaryResult(
            contact_type="agent_partner",
            summary_lines=["Line one.", "Line two."],
            discussed="A referral opportunity for a new listing.",
            clay_commitment="Clay will send the standard co-listing agreement.",
            next_step="None stated.",
        )
        contact_id, runner, ghl_client = self._setup(
            registry, activity_ledger, state_db, clock, tmp_path, summary_result=result,
        )
        summary = runner.run()
        assert summary.notes_written == 1
        note = ghl_client.notes[contact_id][0]
        assert note["body"] == f"{result.discussed}\n{result.clay_commitment}"

    def test_both_absent_fails_closed_and_writes_no_note(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        result = SummaryResult(
            contact_type="agent_partner",
            summary_lines=["Clay committed to sending it today.", "Cory replies Friday."],
            discussed="A referral opportunity for a new listing.",
            clay_commitment="None stated.",
            next_step="None stated.",
        )
        contact_id, runner, ghl_client = self._setup(
            registry, activity_ledger, state_db, clock, tmp_path, summary_result=result,
        )
        summary = runner.run()
        assert summary.notes_written == 0
        assert summary.errors == 1
        assert ghl_client.create_calls == 0
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.summary_status == "failed"
        assert row.error_class == "invalid_structured_summary"
        assert row.note_id is None

    def test_failed_structured_summary_holds_frontier_then_retry_succeeds(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        bad_result = SummaryResult(
            contact_type="agent_partner",
            summary_lines=["Line one.", "Line two."],
            discussed="A referral opportunity for a new listing.",
            clay_commitment="None stated.",
            next_step="None stated.",
        )
        contact_id = "c-1"
        _activate(registry, CANARY_PHONE, contact_id)
        clock.advance(hours=1)
        zdate = utc_to_apple_epoch(datetime(2026, 9, 17, 20, 24, 30, tzinfo=timezone.utc))
        desk_rows = [_desk_row(zdate, CANARY_PHONE, zduration=1306)]
        plaud_records = [_plaud_record("rec-1", start_time="2026-09-17T20:22:59+00:00")]
        ghl_contacts = [{"id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID}]

        class _SwitchingSummarizer:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, *, transcript_segments, contact_context, hermes_home):
                self.calls += 1
                return bad_result if self.calls == 1 else _default_summary_result()

        summarizer = _SwitchingSummarizer()
        runner, ghl_client, transcript = _make_runner(
            registry, activity_ledger, state_db, clock, tmp_path,
            plaud_records=plaud_records, desk_rows=desk_rows, ghl_reader_contacts=ghl_contacts,
            summarizer=summarizer,
        )
        first = runner.run()
        assert first.errors == 1
        assert first.notes_written == 0
        assert ghl_client.create_calls == 0

        second = runner.run()
        assert second.notes_written == 1
        assert ghl_client.create_calls == 1
        row = state_db.get_plaud_summary_state("rec-1")
        assert row.summary_status == "complete"
        assert row.error_class is None
        assert row.note_id is not None

    def test_free_form_summary_lines_never_appear_in_the_note_body(
        self, registry, activity_ledger, state_db, clock, tmp_path
    ) -> None:
        result = SummaryResult(
            contact_type="agent_partner",
            summary_lines=[
                "A wildly different free-form line one.",
                "A wildly different free-form line two.",
            ],
            discussed="A referral opportunity for a new listing.",
            clay_commitment="Clay will send the standard co-listing agreement.",
            next_step="Cory will review the agreement and reply by Friday.",
        )
        contact_id, runner, ghl_client = self._setup(
            registry, activity_ledger, state_db, clock, tmp_path, summary_result=result,
        )
        summary = runner.run()
        assert summary.notes_written == 1
        note = ghl_client.notes[contact_id][0]
        for line in result.summary_lines:
            assert line not in note["body"]
        assert note["body"] == f"{result.discussed}\n{result.clay_commitment}\n{result.next_step}"
