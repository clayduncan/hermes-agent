"""Tests for the OPS-75 v2 iMessage lane of the GHL note mirror
(plugins/team_duncan_contacts/note_mirror.py::mirror_message_event).

Uses the same fake GHL client / real IngestionStateDb pattern as
test_note_mirror.py (the OPS-18 call lane's own test file, left unmodified
by this build). Covers: the exact source-timestamp title in
America/Chicago (CDT/summer, CST/winter, date rollover, fold-safe), the
exact live GHL colors, a body that is exactly the source text with no
timestamp/marker, the local-state-only dedupe fast path, and that no
GHL note body is ever scanned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from plugins.team_duncan_contacts.ingestion_state_db import IngestionStateDb
from plugins.team_duncan_contacts.note_mirror import (
    IMESSAGE_RECEIVED_COLOR,
    IMESSAGE_SENT_COLOR,
    OK_OUTCOMES,
    NoteMirror,
    format_imessage_note_body,
    format_imessage_note_title,
)

OCCURRED_AT = datetime(2026, 9, 22, 14, 3, tzinfo=timezone.utc)

#: The user's own worked examples: an aware UTC instant that is 6:15 PM /
#: 6:18 PM in America/Chicago (CDT) on 2026-09-22.
SENT_EXAMPLE_AT = datetime(2026, 9, 22, 23, 15, tzinfo=timezone.utc)
RECEIVED_EXAMPLE_AT = datetime(2026, 9, 22, 23, 18, tzinfo=timezone.utc)


class FakeGhlClient:
    """Same shape as test_note_mirror.py's FakeGhlClient. Deliberately has
    no list_notes method -- the iMessage lane must never call it."""

    def __init__(self, fail_create_for: set[str] | None = None) -> None:
        self.notes: dict[str, list[dict]] = {}
        self.create_calls = 0
        self.get_note_calls = 0
        self.create_call_kwargs: list[dict] = []
        self._fail_create_for = fail_create_for or set()

    def create_note(
        self, contact_id: str, body: str, *, trigger: str, color: str | None = None,
        pinned: bool = False, title: str | None = None,
        redact_body_in_audit: bool = False,
    ) -> dict:
        assert trigger, "create_note must always receive a non-empty trigger"
        self.create_call_kwargs.append(
            {"color": color, "pinned": pinned, "title": title,
             "redact_body_in_audit": redact_body_in_audit}
        )
        if contact_id in self._fail_create_for:
            raise RuntimeError("simulated GHL outage")
        self.create_calls += 1
        note = {
            "id": f"note-{self.create_calls}", "body": body, "contactId": contact_id,
            "color": color, "pinned": pinned, "title": title,
        }
        self.notes.setdefault(contact_id, []).append(note)
        return note

    def get_note(self, contact_id: str, note_id: str) -> dict | None:
        self.get_note_calls += 1
        for n in self.notes.get(contact_id, []):
            if n["id"] == note_id:
                return n
        return None


@pytest.fixture()
def state_db(tmp_path: Path) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db")


# --- Body: exact text only, no timestamp/marker ---------------------------------


class TestBodyIsTextOnly:
    def test_body_is_exactly_the_text(self) -> None:
        assert format_imessage_note_body(text="Hey, see you at 6") == "Hey, see you at 6"

    def test_body_is_verbatim_only_stripped(self) -> None:
        assert format_imessage_note_body(text="  spaced out  \n") == "spaced out"

    def test_body_has_no_truncation_escaping_or_html(self) -> None:
        raw = "<b>bold</b> & 'quotes' \"more\" 100% <script>alert(1)</script>"
        assert format_imessage_note_body(text=raw) == raw

    def test_body_contains_no_timestamp(self) -> None:
        body = format_imessage_note_body(text="see you soon")
        assert "2026" not in body
        assert "UTC" not in body
        assert "AM" not in body and "PM" not in body

    def test_body_contains_no_imsg_evt_marker(self) -> None:
        body = format_imessage_note_body(text="see you soon")
        assert "imsg-evt:" not in body

    def test_format_imessage_note_body_takes_only_text(self) -> None:
        import inspect

        params = set(inspect.signature(format_imessage_note_body).parameters)
        assert params == {"text"}


# --- Title: exact user examples ---------------------------------------------------


class TestTitleExactExamples:
    def test_sent_exact_title(self) -> None:
        title = format_imessage_note_title(occurred_at=SENT_EXAMPLE_AT, is_from_me=True)
        assert title == "iMessage · Sent · Sep 22 2026, 6:15 PM"

    def test_received_exact_title(self) -> None:
        title = format_imessage_note_title(occurred_at=RECEIVED_EXAMPLE_AT, is_from_me=False)
        assert title == "iMessage · Received · Sep 22 2026, 6:18 PM"


# --- Title: America/Chicago DST correctness ---------------------------------------


class TestTitleTimezoneConversion:
    def test_summer_cdt_conversion(self) -> None:
        # 2026-07-04T23:00:00Z is 2026-07-04 18:00 America/Chicago (CDT, UTC-5).
        at = datetime(2026, 7, 4, 23, 0, tzinfo=timezone.utc)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert title == "iMessage · Sent · Jul 4 2026, 6:00 PM"

    def test_winter_cst_conversion(self) -> None:
        # 2026-01-15T23:00:00Z is 2026-01-15 17:00 America/Chicago (CST, UTC-6).
        at = datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)
        title = format_imessage_note_title(occurred_at=at, is_from_me=False)
        assert title == "iMessage · Received · Jan 15 2026, 5:00 PM"

    def test_date_rolls_over_backward_across_the_utc_day_boundary(self) -> None:
        # 2026-07-05T03:30:00Z is still 2026-07-04 22:30 in Chicago (CDT).
        at = datetime(2026, 7, 5, 3, 30, tzinfo=timezone.utc)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert title == "iMessage · Sent · Jul 4 2026, 10:30 PM"

    def test_fold_safe_around_the_fall_back_transition(self) -> None:
        """2026-11-01 02:00 local is when CDT falls back to CST (1:30 AM
        happens twice in wall-clock terms). Both aware UTC instants below
        are genuinely different points in time -- 06:30Z is 1:30 AM CDT,
        07:30Z is 1:30 AM CST an hour later -- so zoneinfo must resolve
        each to the correct (and here, identical-looking) local wall clock
        without raising or silently picking the wrong offset."""
        before_fallback = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        after_fallback = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
        assert before_fallback != after_fallback

        title_before = format_imessage_note_title(occurred_at=before_fallback, is_from_me=False)
        title_after = format_imessage_note_title(occurred_at=after_fallback, is_from_me=False)

        assert title_before == "iMessage · Received · Nov 1 2026, 1:30 AM"
        assert title_after == "iMessage · Received · Nov 1 2026, 1:30 AM"

        tz = ZoneInfo("America/Chicago")
        assert before_fallback.astimezone(tz).utcoffset() == timedelta(hours=-5)
        assert after_fallback.astimezone(tz).utcoffset() == timedelta(hours=-6)

    def test_requires_timezone_aware_occurred_at(self) -> None:
        naive = datetime(2026, 9, 22, 14, 3)
        with pytest.raises(ValueError):
            format_imessage_note_title(occurred_at=naive, is_from_me=True)


# --- Title: locale independence ----------------------------------------------------


class TestTitleLocaleIndependence:
    def test_title_does_not_depend_on_host_locale(self, monkeypatch) -> None:
        import locale as locale_module

        def _boom(*_a, **_k):
            raise AssertionError("format_imessage_note_title must not consult host locale")

        monkeypatch.setattr(locale_module, "setlocale", _boom)
        title = format_imessage_note_title(occurred_at=SENT_EXAMPLE_AT, is_from_me=True)
        assert title == "iMessage · Sent · Sep 22 2026, 6:15 PM"

    def test_month_is_english_abbreviation_not_strftime_b(self) -> None:
        at = datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert "Dec" in title

    def test_day_has_no_leading_zero(self) -> None:
        at = datetime(2026, 9, 5, 23, 15, tzinfo=timezone.utc)  # Sep 5, 6:15 PM Chicago (CDT)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert "Sep 5 2026" in title
        assert "Sep 05 2026" not in title

    def test_hour_has_no_leading_zero_and_minute_is_always_two_digits(self) -> None:
        at = datetime(2026, 9, 22, 14, 5, tzinfo=timezone.utc)  # 9:05 AM Chicago (CDT)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert "9:05 AM" in title
        assert "09:05 AM" not in title

    def test_am_pm_is_uppercase(self) -> None:
        at = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)  # 11:00 AM Chicago (CDT)
        title = format_imessage_note_title(occurred_at=at, is_from_me=True)
        assert "AM" in title
        assert "am" not in title


# --- Colors by direction -----------------------------------------------------------


class TestColorsByDirection:
    def test_sent_color_is_the_exact_ghl_blue_100(self) -> None:
        assert IMESSAGE_SENT_COLOR == "#d1e9ff"

    def test_received_color_is_the_exact_ghl_gray_100(self) -> None:
        assert IMESSAGE_RECEIVED_COLOR == "#f2f4f7"

    def test_sent_uses_sent_color(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        note = ghl.notes["c-1"][0]
        assert note["color"] == IMESSAGE_SENT_COLOR

    def test_received_uses_received_color(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
            is_from_me=False, trigger="t",
        )
        note = ghl.notes["c-1"][0]
        assert note["color"] == IMESSAGE_RECEIVED_COLOR


# --- End-to-end: mirror_message_event writes body/title/color correctly ----------


class TestMirrorMessageEventWiresBodyTitleColor:
    def test_note_body_is_exactly_the_text(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="Hey, see you at 6",
            occurred_at=SENT_EXAMPLE_AT, is_from_me=True, trigger="t",
        )
        assert ghl.notes["c-1"][0]["body"] == "Hey, see you at 6"

    def test_note_title_is_the_source_timestamp_not_creation_time(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hi", occurred_at=SENT_EXAMPLE_AT,
            is_from_me=True, trigger="t",
        )
        assert ghl.notes["c-1"][0]["title"] == "iMessage · Sent · Sep 22 2026, 6:15 PM"


# --- Create / verify contract, local-state fast path -----------------------------


class TestMirrorMessageEventCreates:
    def test_first_call_creates_exactly_one_note_and_records_complete(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "created"
        assert ghl.create_calls == 1
        assert ghl.get_note_calls == 1
        row = state_db.get_note_mirror("evt-1")
        assert row.status == "complete"
        assert row.note_id == result.note_id

    def test_retry_the_same_event_id_uses_local_state_fast_path(
        self, state_db: IngestionStateDb
    ) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        first = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        second = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert second.outcome == "already_complete"
        assert second.note_id == first.note_id
        assert ghl.create_calls == 1


# --- No marker, no GHL note body scan on any path ---------------------------------


class TestNoMarkerScanning:
    def test_no_list_notes_call_on_first_creation(self, state_db: IngestionStateDb) -> None:
        """The fake client has no list_notes method at all -- if mirror_
        message_event ever called it, this test would fail with an
        AttributeError before the assertion below even runs."""
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "created"
        assert not hasattr(ghl, "list_notes")

    def test_no_list_notes_call_on_retry(self, state_db: IngestionStateDb) -> None:
        ghl = FakeGhlClient()
        mirror = NoteMirror(ghl, state_db)
        mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        result = mirror.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert result.outcome == "already_complete"
        assert not hasattr(ghl, "list_notes")

    def test_lost_local_state_creates_a_second_note_rather_than_scanning(
        self, tmp_path: Path
    ) -> None:
        """With the marker/scan removed, a lost local note_mirror row (with
        the exact-same GHL client/notes state) can no longer be recovered
        by searching GHL -- this is the documented, unavoidable limitation
        (see note_mirror.py's module docstring), proven here rather than
        assumed."""
        ghl = FakeGhlClient()
        state_db_a = IngestionStateDb(tmp_path / "state_a.db")
        mirror_a = NoteMirror(ghl, state_db_a)
        first = mirror_a.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert first.outcome == "created"

        state_db_b = IngestionStateDb(tmp_path / "state_b.db")  # local state "lost"
        mirror_b = NoteMirror(ghl, state_db_b)
        second = mirror_b.mirror_message_event(
            event_id="evt-1", contact_id="c-1", text="hello", occurred_at=OCCURRED_AT,
            is_from_me=True, trigger="t",
        )
        assert second.outcome == "created"
        assert ghl.create_calls == 2
        assert second.note_id != first.note_id


# --- Privacy: iMessage lane still opts into audit redaction, call lane doesn't ----


def test_mirror_message_event_opts_into_audit_redaction(state_db: IngestionStateDb) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert ghl.create_call_kwargs[0]["redact_body_in_audit"] is True


def test_call_lane_create_note_call_never_opts_into_audit_redaction(
    state_db: IngestionStateDb,
) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    mirror.mirror_event(
        event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
        direction="inbound", answered=1, duration_s=60, trigger="t",
    )
    assert ghl.create_call_kwargs[0]["redact_body_in_audit"] is False


# --- Dedupe key is event_id, and two different events with identical text --------


def test_dedupe_key_is_event_id_not_content(state_db: IngestionStateDb) -> None:
    ghl = FakeGhlClient()
    mirror = NoteMirror(ghl, state_db)
    first = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    second = mirror.mirror_message_event(
        event_id="evt-2", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert second.outcome == "created"
    assert second.note_id != first.note_id
    assert ghl.create_calls == 2


# --- Write failure -----------------------------------------------------------------


def test_create_note_failure_is_reported_as_error_and_leaves_state_pending(
    state_db: IngestionStateDb,
) -> None:
    ghl = FakeGhlClient(fail_create_for={"c-1"})
    mirror = NoteMirror(ghl, state_db)
    result = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    assert result.outcome == "error"
    row = state_db.get_note_mirror("evt-1")
    assert row.status == "pending"


# --- OK_OUTCOMES: "recovered" retained for compatibility, never produced ---------


def test_ok_outcomes_still_lists_recovered_for_backward_compatibility() -> None:
    assert "recovered" in OK_OUTCOMES


def test_recovered_is_never_produced_by_mirror_message_event(
    state_db: IngestionStateDb,
) -> None:
    """Exhaustive over every path mirror_message_event can take (create,
    local-state fast path, write failure): none of them ever returns
    'recovered' now that there is no marker to recover by."""
    ghl = FakeGhlClient(fail_create_for={"c-fail"})
    mirror = NoteMirror(ghl, state_db)

    created = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    already_complete = mirror.mirror_message_event(
        event_id="evt-1", contact_id="c-1", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )
    errored = mirror.mirror_message_event(
        event_id="evt-2", contact_id="c-fail", text="hi", occurred_at=OCCURRED_AT,
        is_from_me=True, trigger="t",
    )

    assert {created.outcome, already_complete.outcome, errored.outcome} == {
        "created", "already_complete", "error",
    }


# --- The call lane's own behavior is unaffected by this build --------------------


def test_call_lane_create_note_call_still_never_passes_title(
    state_db: IngestionStateDb,
) -> None:
    """mirror_event (the OPS-18 call lane) must never pass a title kwarg to
    create_note -- the shared _create_and_verify helper must only include
    title in the call when one is actually given."""
    calls: list[dict] = []

    class _RecordingGhl:
        def create_note(self, contact_id, body, **kwargs):
            calls.append(kwargs)
            return {"id": "note-1", "body": body}

        def get_note(self, contact_id, note_id):
            return {"id": note_id, "body": "Incoming call · Answered · 1 min"}

    mirror = NoteMirror(_RecordingGhl(), state_db)
    mirror.mirror_event(
        event_id="evt-1", contact_id="c-1", occurred_at=OCCURRED_AT,
        direction="inbound", answered=1, duration_s=60, trigger="t",
    )
    assert "title" not in calls[0]
