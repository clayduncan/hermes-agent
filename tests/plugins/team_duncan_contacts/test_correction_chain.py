"""Tests for the OPS-18 wrong-contact correction chain.

Real ContactRegistry/ActivityLedger/IngestionStateDb against temp
directories. No live access anywhere. Verifies the achievable correction
path (supersede via the ledger's own `correct_event`, gated on a fresh
`allow` resolution for the corrected contact) preserves prior history
rather than deleting or rewriting it, and that a correction whose fresh
resolution does *not* come back `allow` changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import ActivityLedger
from plugins.team_duncan_contacts.correction_chain import (
    CorrectionFailedError,
    CorrectionNotEligibleError,
    correct_wrong_contact,
)
from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.ingestion_state_db import (
    IngestionStateDb,
    STATUS_COMPLETE,
    STATUS_GRANT_ISSUED,
    STATUS_PENDING_REVIEW,
    STATUS_REPLAYED,
)
from plugins.team_duncan_contacts.registry import ContactRegistry
from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

PHONE_A = "+15551110000"
PHONE_B = "+15552220000"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture()
def registry(tmp_path: Path, clock: _Clock) -> ContactRegistry:
    return ContactRegistry(tmp_path / "registry", team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID, clock=clock)


@pytest.fixture()
def activity_ledger(tmp_path: Path, registry: ContactRegistry) -> ActivityLedger:
    return ActivityLedger(tmp_path / "activity.db", registry)


@pytest.fixture()
def state_db(tmp_path: Path, clock: _Clock) -> IngestionStateDb:
    return IngestionStateDb(tmp_path / "ingestion_state.db", clock=clock)


def _activate(registry, contact_id, phone):
    reader = FakeGhlReader([{
        "id": contact_id, "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": contact_id, "lastName": "X", "phone": phone,
    }])
    prep = registry.prepare_activation(contact_id, reader)
    confirm = registry.confirm_activation(prep.token)
    return confirm.contact_id


@pytest.fixture()
def scenario(registry, activity_ledger, state_db, clock):
    """contact_a admitted (plain `allow`) for an event later found to have
    actually been contact_b's call (source misidentification), with
    contact_b already separately activated and eligible for `allow` too."""
    contact_a = _activate(registry, "c-a", PHONE_A)
    contact_b = _activate(registry, "c-b", PHONE_B)

    event_ts = clock.now + timedelta(days=1)  # after both contacts' cutoff
    ledger_result = activity_ledger.record_event(
        "desk_call", "evt-1", PHONE_A, event_ts,
        {"duration_s": 30, "direction": "inbound", "answered": 1},
    )
    assert ledger_result.outcome == "admitted"

    ins = state_db.insert_pending_review(
        source="desk_call", source_event_id="evt-1", decision="allow",
        match_outcome="unique_match", occurred_at=event_ts.isoformat(),
        duration_s=30, direction="inbound", answered=1, status=STATUS_PENDING_REVIEW,
    )
    state_db.advance_pending_review_stage(
        ins.pending_review_id, STATUS_GRANT_ISSUED, resolved_contact_id=contact_a,
    )
    state_db.advance_pending_review_stage(ins.pending_review_id, STATUS_REPLAYED)
    state_db.advance_pending_review_stage(ins.pending_review_id, STATUS_COMPLETE)
    return {
        "pending_id": ins.pending_review_id, "contact_a": contact_a, "contact_b": contact_b,
        "event_ts": event_ts,
    }


def test_correction_supersedes_without_deleting_old_row(scenario, activity_ledger, state_db) -> None:
    contact_a, contact_b = scenario["contact_a"], scenario["contact_b"]

    old_history_before = activity_ledger.query_full_history(contact_a)
    assert len(old_history_before) == 1
    assert old_history_before[0].state == "active"

    result = correct_wrong_contact(
        state_db=state_db, activity_ledger=activity_ledger,
        pending_review_id=scenario["pending_id"], new_contact_id=contact_b,
        raw_handle=PHONE_B, approval_reason="source re-identified as contact B's line",
    )
    assert result["outcome"] == "corrected"
    assert result["ledger_outcome"] == "corrected"

    # Old row preserved, now explicitly marked superseded -- not deleted.
    old_history_after = activity_ledger.query_full_history(contact_a)
    assert len(old_history_after) == 1
    assert old_history_after[0].event_id == old_history_before[0].event_id
    assert old_history_after[0].state == "superseded"
    assert old_history_after[0].superseded_by is not None

    # New row admitted for the corrected contact.
    new_events = activity_ledger.query_events(contact_b)
    assert len(new_events) == 1
    assert new_events[0].source_event_id == result["new_source_event_id"]
    assert new_events[0].state == "active"

    # pending_review_history is append-only: the original replayed->complete
    # transition is still present, plus a new annotation appended after it.
    history = state_db.get_history(scenario["pending_id"])
    assert any(
        h["from_status"] == STATUS_REPLAYED and h["to_status"] == STATUS_COMPLETE
        for h in history
    )
    assert "corrected_to" in history[-1]["detail"]
    assert history[-1]["from_status"] == STATUS_COMPLETE
    assert history[-1]["to_status"] == STATUS_COMPLETE


def test_correction_is_idempotent_on_retry(scenario, activity_ledger, state_db) -> None:
    contact_b = scenario["contact_b"]
    first = correct_wrong_contact(
        state_db=state_db, activity_ledger=activity_ledger,
        pending_review_id=scenario["pending_id"], new_contact_id=contact_b,
        raw_handle=PHONE_B, approval_reason="reason", correction_seq=1,
    )
    second = correct_wrong_contact(
        state_db=state_db, activity_ledger=activity_ledger,
        pending_review_id=scenario["pending_id"], new_contact_id=contact_b,
        raw_handle=PHONE_B, approval_reason="reason", correction_seq=1,
    )
    assert first["new_source_event_id"] == second["new_source_event_id"]
    assert second["ledger_outcome"] == "noop"
    # Still exactly one admitted row for the corrected contact.
    assert len(activity_ledger.query_events(contact_b)) == 1


def test_correction_fails_closed_when_fresh_resolution_not_allow(scenario, activity_ledger, state_db) -> None:
    contact_a = scenario["contact_a"]
    with pytest.raises(CorrectionFailedError):
        correct_wrong_contact(
            state_db=state_db, activity_ledger=activity_ledger,
            pending_review_id=scenario["pending_id"], new_contact_id="c-unregistered",
            raw_handle="+19998887777", approval_reason="bogus handle, no match",
        )
    # Nothing changed: old row still active, no grant revoked.
    old_history = activity_ledger.query_full_history(contact_a)
    assert len(old_history) == 1
    assert old_history[0].state == "active"


def test_correction_rejects_same_contact(scenario, activity_ledger, state_db) -> None:
    with pytest.raises(CorrectionNotEligibleError):
        correct_wrong_contact(
            state_db=state_db, activity_ledger=activity_ledger,
            pending_review_id=scenario["pending_id"], new_contact_id=scenario["contact_a"],
            raw_handle=PHONE_A, approval_reason="noop",
        )


def test_correction_rejects_non_terminal_pending_review(registry, activity_ledger, state_db, clock) -> None:
    contact_b = _activate(registry, "c-b", PHONE_B)
    ins = state_db.insert_pending_review(
        source="plaud", source_event_id="evt-open", decision="review_required",
        match_outcome="zero_match", occurred_at=clock.now.isoformat(),
        duration_s=10, direction=None, answered=None, status=STATUS_PENDING_REVIEW,
    )
    with pytest.raises(CorrectionNotEligibleError):
        correct_wrong_contact(
            state_db=state_db, activity_ledger=activity_ledger,
            pending_review_id=ins.pending_review_id, new_contact_id=contact_b,
            raw_handle=PHONE_B, approval_reason="too early",
        )
