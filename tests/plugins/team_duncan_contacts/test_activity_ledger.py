"""Tests for ActivityLedger (OPS-74).

All tests use fake registries and temp directories. No live GHL calls.
No real credentials or live profile data touched.

Coverage aligned to spec §6 invariants and §8 backup/restore proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.activity_ledger import (
    TEAM_DUNCAN_LOCATION_ID,
    ActivityLedger,
    EventRow,
    OverrideHistoryRow,
    _event_id,
    _override_id,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOC = TEAM_DUNCAN_LOCATION_ID  # "abi5iDumIeysZCvWt99r"
_CONTACT_A = "contactAAA"
_CONTACT_B = "contactBBB"
_HANDLE_A = "handle-alpha"
_HANDLE_B = "handle-bravo"

_T_ACTIVATED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_T_BEFORE = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
_T_AFTER = datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake registry
# ---------------------------------------------------------------------------


class _FakeResolveResult:
    def __init__(
        self,
        decision: str,
        contact_id: str | None = None,
        location_id: str | None = None,
    ) -> None:
        self.decision = decision
        self.ghl_contact_id = contact_id
        self.location_id = location_id


class _FakeRegistry:
    """Configurable fake registry for unit tests."""

    def __init__(self) -> None:
        # map handle -> (decision, contact_id, location_id)
        self._responses: dict[str, tuple[str, str | None, str | None]] = {}
        self._calls: list[tuple[str, datetime]] = []

    def set_allow(self, handle: str, contact_id: str, location_id: str = _LOC) -> None:
        self._responses[handle] = ("allow", contact_id, location_id)

    def set_deny_pre_activation(
        self, handle: str, contact_id: str, location_id: str = _LOC
    ) -> None:
        self._responses[handle] = ("deny_pre_activation", contact_id, location_id)

    def set_decision(
        self,
        handle: str,
        decision: str,
        contact_id: str | None = None,
        location_id: str | None = None,
    ) -> None:
        self._responses[handle] = (decision, contact_id, location_id)

    def resolve_event(self, raw_handle: str, event_ts: datetime) -> _FakeResolveResult:
        self._calls.append((raw_handle, event_ts))
        if raw_handle not in self._responses:
            return _FakeResolveResult("review_required")
        decision, cid, lid = self._responses[raw_handle]
        return _FakeResolveResult(decision, cid, lid)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def last_call_handle(self) -> str | None:
        return self._calls[-1][0] if self._calls else None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_ledger(tmp_path: Path):
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    reg.set_allow(_HANDLE_B, _CONTACT_B)
    db = tmp_path / "activity.db"
    ledger = ActivityLedger(db, reg)
    return ledger, reg, tmp_path


# ---------------------------------------------------------------------------
# Deterministic ID helpers
# ---------------------------------------------------------------------------


def test_event_id_deterministic():
    a = _event_id("ghl_email", "ev-001")
    b = _event_id("ghl_email", "ev-001")
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_override_id_deterministic():
    a = _override_id("cABC", "ghl_email", "ev-001")
    b = _override_id("cABC", "ghl_email", "ev-001")
    assert a == b
    assert len(a) == 64


def test_event_id_differs_by_source():
    a = _event_id("ghl_email", "ev-001")
    b = _event_id("ghl_call", "ev-001")
    assert a != b


def test_event_id_differs_by_source_event_id():
    a = _event_id("ghl_email", "ev-001")
    b = _event_id("ghl_email", "ev-002")
    assert a != b


# ---------------------------------------------------------------------------
# record_event: normal path
# ---------------------------------------------------------------------------


def test_record_event_admitted(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    result = ledger.record_event(
        "ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {}
    )
    assert result.outcome == "admitted"
    assert result.event_id == _event_id("ghl_email", "ev-001")


def test_record_event_idempotent(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    r1 = ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    r2 = ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    assert r1.outcome == "admitted"
    assert r2.outcome == "admitted"
    # Only one row
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1


def test_record_event_discard_review_required(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_decision("unknown-handle", "review_required")
    result = ledger.record_event("ghl_email", "ev-x", "unknown-handle", _T_AFTER, {})
    assert result.outcome == "discarded"
    assert result.decision == "review_required"


def test_record_event_discard_deny_paused(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_decision("paused-h", "deny_paused", _CONTACT_A, _LOC)
    result = ledger.record_event("ghl_email", "ev-x", "paused-h", _T_AFTER, {})
    assert result.outcome == "discarded"


def test_record_event_discard_deny_retired(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_decision("retired-h", "deny_retired", _CONTACT_A, _LOC)
    result = ledger.record_event("ghl_email", "ev-x", "retired-h", _T_AFTER, {})
    assert result.outcome == "discarded"


def test_record_event_discard_wrong_location(tmp_path: Path):
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A, location_id="other-loc-xyz")
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    result = ledger.record_event("ghl_email", "ev-x", _HANDLE_A, _T_AFTER, {})
    assert result.outcome == "discarded"
    assert result.decision == "wrong_location"


def test_record_event_no_row_on_discard(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-x", "unknown-handle", _T_AFTER, {})
    rows = ledger.query_events(_CONTACT_A)
    assert rows == []


# ---------------------------------------------------------------------------
# record_event: pre-activation / override path
# ---------------------------------------------------------------------------


def test_record_event_deny_pre_activation_no_grant_discards(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert result.decision == "deny_pre_activation"
    assert ledger.query_events(_CONTACT_A) == []


def test_record_event_override_admitted(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)

    # Grant first
    gr = ledger.grant_override(
        _CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved by clay"
    )
    assert gr.outcome == "created"

    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "override_admitted"

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].state == "override_admitted"
    assert rows[0].override_id is not None


def test_record_event_revoked_grant_discards(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)

    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(_CONTACT_A) == []


def test_grant_exactness_different_source_event(tmp_ledger):
    """Grant for ev-001 does not admit ev-002."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)

    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-001", "approved")
    result = ledger.record_event("ghl_email", "ev-002", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"


def test_grant_exactness_different_source(tmp_ledger):
    """Grant for ghl_email does not admit ghl_call."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)

    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-001", "approved")
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    result = ledger.record_event("ghl_call", "ev-001", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"


# ---------------------------------------------------------------------------
# Invariant: no raw handle in any stored row
# ---------------------------------------------------------------------------


def test_no_raw_handle_in_contact_events(tmp_ledger):
    """raw_handle (a phone number) is not stored in any event column.

    Structural protection: contact_id comes from resolve_event, never raw_handle.
    Sanitizer protection: phone numbers in provenance are redacted.
    """
    ledger, reg, _ = tmp_ledger
    raw = "555-123-4567"  # phone-like so sanitizer catches it if it leaks
    reg.set_allow(raw, _CONTACT_A)
    ledger.record_event("ghl_email", "ev-001", raw, _T_AFTER, {"note": raw})

    export = ledger.export_ledger()
    for row in export["contact_events"]:
        for val in row.values():
            if isinstance(val, str):
                assert raw not in val, f"Raw handle found in row: {row}"


def test_no_raw_handle_in_export(tmp_ledger):
    """Phone-like raw handle is sanitized out of provenance before storage."""
    ledger, reg, _ = tmp_ledger
    raw = "555-999-8888"  # phone-like so sanitizer redacts it from provenance
    reg.set_allow(raw, _CONTACT_A)
    provenance = {"info": raw, "nested": {"phone": raw}}
    ledger.record_event("ghl_email", "ev-001", raw, _T_AFTER, provenance)

    export = ledger.export_ledger()
    export_str = json.dumps(export)
    assert raw not in export_str


# ---------------------------------------------------------------------------
# Invariant: contact_id from resolve_event only
# ---------------------------------------------------------------------------


def test_contact_id_from_registry(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].contact_id == _CONTACT_A


# ---------------------------------------------------------------------------
# Invariant: location exclusivity
# ---------------------------------------------------------------------------


def test_location_id_on_every_row(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    rows = ledger.query_events(_CONTACT_A)
    assert rows[0].location_id == _LOC


# ---------------------------------------------------------------------------
# correct_event
# ---------------------------------------------------------------------------


def test_correct_event_supersedes_old(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})

    cr = ledger.correct_event(
        "ghl_email", "ev-001", "ev-001-corrected", _HANDLE_A, _T_AFTER, {}
    )
    assert cr.outcome == "corrected"
    assert cr.new_event_id == _event_id("ghl_email", "ev-001-corrected")
    assert cr.old_event_id == _event_id("ghl_email", "ev-001")

    active = ledger.query_events(_CONTACT_A)
    assert len(active) == 1
    assert active[0].source_event_id == "ev-001-corrected"

    full = ledger.query_full_history(_CONTACT_A)
    assert len(full) == 2
    states = {r.source_event_id: r.state for r in full}
    assert states["ev-001"] == "superseded"
    assert states["ev-001-corrected"] == "active"


def test_correct_event_idempotent(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})

    cr1 = ledger.correct_event(
        "ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {}
    )
    cr2 = ledger.correct_event(
        "ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {}
    )
    assert cr1.outcome == "corrected"
    assert cr2.outcome == "noop"
    # Still only 2 rows total
    assert len(ledger.query_full_history(_CONTACT_A)) == 2


def test_correct_event_superseded_by_set(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    full = ledger.query_full_history(_CONTACT_A)
    old = next(r for r in full if r.source_event_id == "ev-001")
    new = next(r for r in full if r.source_event_id == "ev-001c")
    assert old.superseded_by == new.event_id
    assert old.state == "superseded"


# ---------------------------------------------------------------------------
# grant_override
# ---------------------------------------------------------------------------


def test_grant_override_created(tmp_ledger):
    ledger, _, _ = tmp_ledger
    gr = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    assert gr.outcome == "created"
    assert gr.override_id is not None


def test_grant_override_idempotent(tmp_ledger):
    ledger, _, _ = tmp_ledger
    gr1 = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    gr2 = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    assert gr1.outcome == "created"
    assert gr2.outcome == "noop"
    assert gr1.override_id == gr2.override_id


def test_grant_override_wrong_location_raises(tmp_ledger):
    ledger, _, _ = tmp_ledger
    with pytest.raises(ValueError, match="Team Duncan location"):
        ledger.grant_override(
            _CONTACT_A, "wrong-loc-xyz", "ghl_email", "ev-pre", "reason"
        )


def test_grant_override_reactivates_revoked(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    gr = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "re-approved")
    assert gr.outcome == "reactivated"

    history = ledger.query_override_history(_CONTACT_A)
    assert len(history) == 1
    transitions = [h["transition"] for h in history[0].history]
    assert transitions == ["approved", "revoked", "approved"]


def test_grant_override_one_grant_per_event(tmp_ledger):
    """UNIQUE (contact_id, location_id, source, source_event_id) enforced."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "first")
    # Same four-part key; should be noop, not an error
    gr = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "second")
    assert gr.outcome == "noop"


# ---------------------------------------------------------------------------
# revoke_override
# ---------------------------------------------------------------------------


def test_revoke_override_sets_revoked(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    rv = ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    assert rv.outcome == "revoked"

    history = ledger.query_override_history(_CONTACT_A)
    assert history[0].state == "revoked"


def test_revoke_override_not_found(tmp_ledger):
    ledger, _, _ = tmp_ledger
    rv = ledger.revoke_override(_CONTACT_A, "ghl_email", "nonexistent")
    assert rv.outcome == "not_found"


def test_revoke_override_already_revoked(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    rv = ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    assert rv.outcome == "already_revoked"


def test_revoke_does_not_delete_admitted_events(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)

    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].state == "override_admitted"


# ---------------------------------------------------------------------------
# query_events
# ---------------------------------------------------------------------------


def test_query_events_chronological_order(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-c", _HANDLE_A, t3, {})
    ledger.record_event("ghl_email", "ev-a", _HANDLE_A, t1, {})
    ledger.record_event("ghl_email", "ev-b", _HANDLE_A, t2, {})
    rows = ledger.query_events(_CONTACT_A)
    times = [r.occurred_at for r in rows]
    assert times == sorted(times)


def test_query_events_excludes_superseded(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].source_event_id == "ev-001c"


def test_query_events_after_occurred_at(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-a", _HANDLE_A, t1, {})
    ledger.record_event("ghl_email", "ev-b", _HANDLE_A, t2, {})
    rows = ledger.query_events(_CONTACT_A, after_occurred_at=t1.isoformat())
    assert len(rows) == 1
    assert rows[0].source_event_id == "ev-b"


def test_query_events_limit(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    for i in range(5):
        t = datetime(2026, 6, 1, i, 0, 0, tzinfo=timezone.utc)
        ledger.record_event("ghl_email", f"ev-{i}", _HANDLE_A, t, {})
    rows = ledger.query_events(_CONTACT_A, limit=3)
    assert len(rows) == 3


def test_query_events_event_id_tiebreak(tmp_ledger):
    """Same occurred_at: ordering by event_id ASC is deterministic."""
    ledger, reg, _ = tmp_ledger
    same_t = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-z", _HANDLE_A, same_t, {})
    ledger.record_event("ghl_email", "ev-a", _HANDLE_A, same_t, {})

    rows = ledger.query_events(_CONTACT_A)
    eid_z = _event_id("ghl_email", "ev-z")
    eid_a = _event_id("ghl_email", "ev-a")
    returned_eids = [r.event_id for r in rows]
    assert returned_eids == sorted([eid_z, eid_a])


def test_query_events_state_visible(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})

    reg.set_allow(_HANDLE_A, _CONTACT_A)
    ledger.record_event("ghl_email", "ev-post", _HANDLE_A, _T_AFTER, {})

    rows = ledger.query_events(_CONTACT_A)
    states = {r.source_event_id: r.state for r in rows}
    assert states["ev-pre"] == "override_admitted"
    assert states["ev-post"] == "active"


# ---------------------------------------------------------------------------
# query_full_history
# ---------------------------------------------------------------------------


def test_query_full_history_includes_superseded(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    full = ledger.query_full_history(_CONTACT_A)
    assert len(full) == 2
    s = {r.source_event_id: r.state for r in full}
    assert "superseded" in s.values()
    assert "active" in s.values()


# ---------------------------------------------------------------------------
# query_override_history
# ---------------------------------------------------------------------------


def test_query_override_history_full_chain(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "first")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "second")

    history = ledger.query_override_history(_CONTACT_A)
    assert len(history) == 1
    h = history[0]
    assert h.contact_id == _CONTACT_A
    transitions = [e["transition"] for e in h.history]
    assert transitions == ["approved", "revoked", "approved"]
    assert h.state == "active"


def test_query_override_history_no_raw_handles(tmp_ledger):
    """Approval reason containing a phone-like value is redacted by sanitizer."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(
        _CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved by 555-123-4567"
    )
    history = ledger.query_override_history(_CONTACT_A)
    assert "555-123-4567" not in history[0].approval_reason


# ---------------------------------------------------------------------------
# export_ledger
# ---------------------------------------------------------------------------


def test_export_ledger_all_tables(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    export = ledger.export_ledger()
    assert "contact_events" in export
    assert "approved_overrides" in export
    assert "override_history" in export
    assert len(export["contact_events"]) == 1
    assert len(export["approved_overrides"]) == 1
    assert len(export["override_history"]) >= 2  # approved + revoked


def test_export_ledger_json_serializable(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {"k": "v"})
    export = ledger.export_ledger()
    # Should not raise
    json.dumps(export)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def test_backup_creates_copy(tmp_ledger):
    ledger, reg, tmp_path = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    backup_path = tmp_path / "backup.db"
    ledger.backup(backup_path)
    assert backup_path.exists()


def test_backup_rejects_live_path(tmp_ledger):
    ledger, reg, tmp_path = tmp_ledger
    with pytest.raises(ValueError, match="live DB path"):
        ledger.backup(tmp_path / "activity.db")


def test_backup_rejects_existing_dest(tmp_ledger):
    ledger, reg, tmp_path = tmp_ledger
    dest = tmp_path / "existing.db"
    dest.write_bytes(b"")
    with pytest.raises(ValueError, match="already exists"):
        ledger.backup(dest)


# ---------------------------------------------------------------------------
# test_backup_throwaway_restore (spec §8 requirement)
# ---------------------------------------------------------------------------


def test_backup_throwaway_restore(tmp_path: Path):
    """Full restore-proof test as required by spec §8.

    1. Create ledger with known events including override_admitted.
    2. Call backup(backup.db).
    3. Open backup.db as a fresh ActivityLedger (the throwaway).
    4. Assert query_events returns identical rows in identical order.
    5. Assert export_ledger() contains no raw handles.
    6. Assert override_history rows match the originals.
    7. Delete the original DB file.
    8. Assert throwaway remains fully functional (row count and event_ids match).
    """
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)

    live_db = tmp_path / "live" / "activity.db"
    ledger = ActivityLedger(live_db, reg)

    # Event 1: normal
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, t1, {"ref": "ref-001"})

    # Event 2: override_admitted
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_call", "call-pre", "pre-activation approved")
    ledger.record_event("ghl_call", "call-pre", _HANDLE_A, _T_BEFORE, {})

    # Step 2: backup
    backup_db = tmp_path / "backup.db"
    ledger.backup(backup_db)

    # Step 3: open backup as throwaway
    throwaway = ActivityLedger(backup_db, reg)

    # Step 4: query_events matches
    live_rows = ledger.query_events(_CONTACT_A)
    backup_rows = throwaway.query_events(_CONTACT_A)
    assert len(live_rows) == len(backup_rows)
    for lr, br in zip(live_rows, backup_rows):
        assert lr.event_id == br.event_id
        assert lr.state == br.state
        assert lr.occurred_at == br.occurred_at

    # Step 5: export_ledger contains no raw handles
    raw_vals = [_HANDLE_A, _HANDLE_B]
    export = throwaway.export_ledger()
    export_str = json.dumps(export)
    for raw in raw_vals:
        assert raw not in export_str

    # Step 6: override_history rows match
    live_oh = ledger.query_override_history(_CONTACT_A)
    backup_oh = throwaway.query_override_history(_CONTACT_A)
    assert len(live_oh) == len(backup_oh)
    for lo, bo in zip(live_oh, backup_oh):
        assert lo.override_id == bo.override_id
        assert lo.state == bo.state
        assert [h["transition"] for h in lo.history] == [h["transition"] for h in bo.history]

    # Step 7: delete original DB
    live_db.unlink()
    assert not live_db.exists()

    # Step 8: throwaway remains functional
    throwaway_rows = throwaway.query_events(_CONTACT_A)
    assert len(throwaway_rows) == len(live_rows)
    throwaway_eids = {r.event_id for r in throwaway_rows}
    live_eids = {r.event_id for r in live_rows}
    assert throwaway_eids == live_eids


# ---------------------------------------------------------------------------
# Overlapping-window idempotency
# ---------------------------------------------------------------------------


def test_overlapping_window_no_duplicates(tmp_ledger):
    """Two overlapping ingestion batches produce no duplicate rows."""
    ledger, reg, _ = tmp_ledger
    t = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

    batch_1 = ["ev-a", "ev-b", "ev-c"]
    batch_2 = ["ev-b", "ev-c", "ev-d"]  # overlaps on ev-b, ev-c

    for eid in batch_1:
        ledger.record_event("ghl_email", eid, _HANDLE_A, t, {})

    for eid in batch_2:
        ledger.record_event("ghl_email", eid, _HANDLE_A, t, {})

    rows = ledger.query_events(_CONTACT_A)
    source_event_ids = [r.source_event_id for r in rows]
    assert sorted(source_event_ids) == sorted(["ev-a", "ev-b", "ev-c", "ev-d"])
    # No duplicates
    assert len(source_event_ids) == len(set(source_event_ids))


# ---------------------------------------------------------------------------
# Sanitizer applied to provenance and approval_reason
# ---------------------------------------------------------------------------


def test_provenance_sanitized_before_storage(tmp_ledger):
    ledger, reg, _ = tmp_ledger
    phone = "555-867-5309"
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {"phone": phone})
    rows = ledger.query_events(_CONTACT_A)
    assert phone not in rows[0].provenance_json


def test_approval_reason_sanitized(tmp_ledger):
    ledger, _, _ = tmp_ledger
    phone = "555-867-5309"
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", f"approved: {phone}")
    history = ledger.query_override_history(_CONTACT_A)
    assert phone not in history[0].approval_reason


# ---------------------------------------------------------------------------
# No destructive API methods
# ---------------------------------------------------------------------------


def test_no_delete_event_method():
    assert not hasattr(ActivityLedger, "delete_event")


def test_no_void_event_method():
    assert not hasattr(ActivityLedger, "void_event")


def test_no_clear_ledger_method():
    assert not hasattr(ActivityLedger, "clear_ledger")


def test_no_truncate_method():
    assert not hasattr(ActivityLedger, "truncate")


def test_no_restore_ledger_method():
    assert not hasattr(ActivityLedger, "restore_ledger")


# ---------------------------------------------------------------------------
# Supersession chain integrity
# ---------------------------------------------------------------------------


def test_supersession_is_atomic(tmp_ledger):
    """Both the new row write and the old row update happen in one transaction."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    full = ledger.query_full_history(_CONTACT_A)
    old = next(r for r in full if r.source_event_id == "ev-001")
    new = next(r for r in full if r.source_event_id == "ev-001c")
    # Both changes present: old is superseded, new exists
    assert old.state == "superseded"
    assert old.superseded_by == new.event_id
    assert new.state == "active"


# ---------------------------------------------------------------------------
# approved_by and actor always 'clay'
# ---------------------------------------------------------------------------


def test_approved_by_always_clay(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    export = ledger.export_ledger()
    for ov in export["approved_overrides"]:
        assert ov["approved_by"] == "clay"


def test_actor_always_clay(tmp_ledger):
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    export = ledger.export_ledger()
    for row in export["override_history"]:
        assert row["actor"] == "clay"


# ===========================================================================
# §1. Stable source-event identifiers and exact GHL contact ownership
# ===========================================================================


def test_stable_event_id(tmp_path: Path):
    """Same source+source_event_id → same event_id on first ingest, re-ingest,
    and across a freshly constructed ActivityLedger over the same DB file.
    """
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    db = tmp_path / "activity.db"
    ledger = ActivityLedger(db, reg)

    r1 = ledger.record_event("ghl_email", "ev-stable", _HANDLE_A, _T_AFTER, {})
    assert r1.outcome == "admitted"
    first_eid = r1.event_id
    assert first_eid == _event_id("ghl_email", "ev-stable")

    # Re-ingest is idempotent and returns the same event_id
    r2 = ledger.record_event("ghl_email", "ev-stable", _HANDLE_A, _T_AFTER, {})
    assert r2.event_id == first_eid

    # Fresh ActivityLedger instance over the same DB file
    ledger2 = ActivityLedger(db, reg)
    rows = ledger2.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].event_id == first_eid


def test_ghl_ownership(tmp_ledger):
    """contact_id on the persisted row equals the ghl_contact_id from resolve_event."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].contact_id == _CONTACT_A  # _CONTACT_A is what the stub returns


def test_wrong_contact_cannot_be_injected(tmp_path: Path):
    """contact_id comes exclusively from resolve_event; no caller override path."""
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)

    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    rows = ledger.query_events(_CONTACT_A)
    assert rows[0].contact_id == _CONTACT_A

    # Contact B has no events (the handle for A cannot inject B's contact_id)
    assert ledger.query_events(_CONTACT_B) == []


# ===========================================================================
# §2. Chronological ordering
# ===========================================================================


def test_chronological_ordering(tmp_ledger):
    """Rows inserted out-of-order are returned in occurred_at ascending order."""
    ledger, reg, _ = tmp_ledger
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-c", _HANDLE_A, t3, {})
    ledger.record_event("ghl_email", "ev-a", _HANDLE_A, t1, {})
    ledger.record_event("ghl_email", "ev-b", _HANDLE_A, t2, {})
    rows = ledger.query_events(_CONTACT_A)
    assert [r.source_event_id for r in rows] == ["ev-a", "ev-b", "ev-c"]


def test_chronological_tie_break(tmp_ledger):
    """Two rows with identical occurred_at are returned in event_id ASC order,
    verified by querying twice to confirm determinism.
    """
    ledger, reg, _ = tmp_ledger
    same_t = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-z", _HANDLE_A, same_t, {})
    ledger.record_event("ghl_email", "ev-a", _HANDLE_A, same_t, {})

    rows1 = ledger.query_events(_CONTACT_A)
    rows2 = ledger.query_events(_CONTACT_A)
    eids1 = [r.event_id for r in rows1]
    eids2 = [r.event_id for r in rows2]
    assert eids1 == eids2  # deterministic
    assert eids1 == sorted(eids1)  # ascending by event_id


def test_override_admitted_in_chronological_position(tmp_ledger):
    """An override_admitted event whose occurred_at < activated_at appears in
    correct chronological position and is distinguished by state='override_admitted'.
    """
    ledger, reg, _ = tmp_ledger
    t_pre = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    t_post = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)

    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")

    # Insert post-activation event first
    reg.set_allow(_HANDLE_A, _CONTACT_A)
    ledger.record_event("ghl_email", "ev-post", _HANDLE_A, t_post, {})

    # Insert pre-activation event second (with grant)
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, t_pre, {})

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 2
    assert rows[0].source_event_id == "ev-pre"
    assert rows[0].state == "override_admitted"
    assert rows[1].source_event_id == "ev-post"


# ===========================================================================
# §3. Idempotent replay and overlapping-window proof
# ===========================================================================


def test_idempotent_replay(tmp_ledger):
    """record_event called twice with same (source, source_event_id) → one row."""
    ledger, reg, _ = tmp_ledger
    r1 = ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    r2 = ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    assert r1.outcome == "admitted"
    assert r2.outcome == "admitted"
    assert r1.event_id == r2.event_id
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1


def test_overlapping_window(tmp_ledger):
    """Batch A (events 1-5) + Batch B (events 3-7) → exactly 7 rows, no dupes."""
    ledger, reg, _ = tmp_ledger
    t = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    batch_a = [f"ev-{i}" for i in range(1, 6)]
    batch_b = [f"ev-{i}" for i in range(3, 8)]

    for eid in batch_a:
        ledger.record_event("ghl_email", eid, _HANDLE_A, t, {})
    for eid in batch_b:
        ledger.record_event("ghl_email", eid, _HANDLE_A, t, {})

    rows = ledger.query_events(_CONTACT_A)
    source_eids = [r.source_event_id for r in rows]
    assert len(source_eids) == 7
    assert len(set(source_eids)) == 7  # no duplicates
    for eid in ["ev-3", "ev-4", "ev-5"]:
        assert source_eids.count(eid) == 1


def test_grant_idempotent(tmp_ledger):
    """grant_override twice on active grant → one approved_overrides row, one history row."""
    ledger, _, _ = tmp_ledger
    gr1 = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    gr2 = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "reason")
    assert gr1.outcome == "created"
    assert gr2.outcome == "noop"
    assert gr1.override_id == gr2.override_id

    export = ledger.export_ledger()
    assert len(export["approved_overrides"]) == 1
    assert len(export["override_history"]) == 1


def test_grant_reactivate_after_revoke(tmp_ledger):
    """grant_override after revoke_override appends 'approved' and sets state='active'."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "initial")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    gr = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "re-approved")
    assert gr.outcome == "reactivated"

    history = ledger.query_override_history(_CONTACT_A)
    assert len(history) == 1
    transitions = [h["transition"] for h in history[0].history]
    assert transitions == ["approved", "revoked", "approved"]
    assert history[0].state == "active"


# ===========================================================================
# §4. Corrections and supersession retained
# ===========================================================================


def test_correction_supersession(tmp_ledger):
    """correct_event: old row superseded, new row active; query_events returns new only."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})

    cr = ledger.correct_event("ghl_email", "ev-001", "ev-001-corrected", _HANDLE_A, _T_AFTER, {})
    assert cr.outcome == "corrected"

    full = ledger.query_full_history(_CONTACT_A)
    old = next(r for r in full if r.source_event_id == "ev-001")
    new = next(r for r in full if r.source_event_id == "ev-001-corrected")
    assert old.state == "superseded"
    assert old.superseded_by == new.event_id
    assert new.state == "active"

    active = ledger.query_events(_CONTACT_A)
    assert len(active) == 1
    assert active[0].source_event_id == "ev-001-corrected"


def test_correction_idempotent(tmp_ledger):
    """Calling correct_event twice with identical args → same single correction, no duplicate."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})

    cr1 = ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})
    cr2 = ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})
    assert cr1.outcome == "corrected"
    assert cr2.outcome == "noop"
    assert len(ledger.query_full_history(_CONTACT_A)) == 2


def test_correction_atomicity(tmp_ledger, monkeypatch):
    """Simulated failure mid-correction leaves DB in pre-correction state (no partial state)."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})

    should_fail = [True]
    original_connect = ledger._connect

    class _FailingConn:
        """Wrapper that raises on the UPDATE that marks a row 'superseded'."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if should_fail[0] and "SET state='superseded'" in sql:
                should_fail[0] = False
                raise sqlite3.OperationalError("simulated mid-correction failure")
            return self._conn.execute(sql, params)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *args):
            return self._conn.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(ledger, "_connect", lambda: _FailingConn(original_connect()))

    with pytest.raises(sqlite3.OperationalError):
        ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    # After the failure, DB must be in pre-correction state (only original row)
    full = ledger.query_full_history(_CONTACT_A)
    assert len(full) == 1
    assert full[0].state == "active"
    assert full[0].source_event_id == "ev-001"


def test_query_full_history_shows_superseded(tmp_ledger):
    """query_full_history includes superseded row; query_events returns only correction."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    full = ledger.query_full_history(_CONTACT_A)
    assert len(full) == 2
    states = {r.source_event_id: r.state for r in full}
    assert states["ev-001"] == "superseded"
    assert states["ev-001c"] == "active"

    active = ledger.query_events(_CONTACT_A)
    assert len(active) == 1
    assert active[0].source_event_id == "ev-001c"


# ===========================================================================
# §5. Source provenance visible
# ===========================================================================


def test_source_provenance(tmp_ledger):
    """source and provenance_json are present and non-empty on every persisted row."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {"k": "v"})
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].source == "ghl_email"
    assert rows[0].provenance_json is not None
    assert rows[0].provenance_json not in ("", "null")


_CANARY_PHONE = "+15550001234"


def test_canary_handle_absent_from_ledger(tmp_path: Path):
    """Canary phone passed as raw_handle does not appear in any column of any row."""
    reg = _FakeRegistry()
    reg.set_allow(_CANARY_PHONE, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    ledger.record_event("ghl_email", "ev-001", _CANARY_PHONE, _T_AFTER, {"note": "data"})

    rows = ledger.query_events(_CONTACT_A)
    for row in rows:
        for val in [
            row.event_id, row.contact_id, row.location_id, row.source,
            row.source_event_id, row.occurred_at, row.ingested_at, row.state,
            str(row.superseded_by or ""), str(row.override_id or ""),
            row.provenance_json,
        ]:
            assert _CANARY_PHONE not in val, f"Canary phone found in field: {val!r}"


def test_sanitizer_applied_to_provenance(tmp_ledger):
    """provenance_json containing a raw phone is sanitized before write."""
    ledger, reg, _ = tmp_ledger
    phone = "555-867-5309"
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {"phone": phone})
    rows = ledger.query_events(_CONTACT_A)
    assert phone not in rows[0].provenance_json


def test_canary_absent_from_export(tmp_path: Path):
    """Canary phone passed as raw_handle does not appear in export_ledger() output."""
    canary = "+15550002345"
    reg = _FakeRegistry()
    reg.set_allow(canary, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    ledger.record_event("ghl_email", "ev-001", canary, _T_AFTER, {"data": "value"})

    export = ledger.export_ledger()
    export_str = json.dumps(export)
    assert canary not in export_str


def test_canary_absent_from_grant_record(tmp_ledger):
    """Canary phone in approval_reason is sanitized; grant columns contain no raw handle."""
    ledger, _, _ = tmp_ledger
    canary = "555-991-0000"
    ledger.grant_override(
        _CONTACT_A, _LOC, "ghl_email", "ev-pre",
        f"approved: {canary}"
    )
    history = ledger.query_override_history(_CONTACT_A)
    assert len(history) == 1
    assert canary not in history[0].approval_reason
    assert canary not in history[0].contact_id
    assert canary not in history[0].source
    assert canary not in history[0].source_event_id


# ===========================================================================
# §6. Backup and real restore proof
# (test_backup_rejects_live_path, test_backup_rejects_existing_dest, and
#  test_backup_throwaway_restore are defined above in the backup section)
# ===========================================================================


def test_sqlite_backup_round_trip(tmp_ledger):
    """After backup, fresh ActivityLedger over backup file returns identical rows in order."""
    ledger, reg, tmp_path = tmp_ledger
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, t1, {})
    ledger.record_event("ghl_email", "ev-002", _HANDLE_A, t2, {})

    backup_path = tmp_path / "round_trip.db"
    ledger.backup(backup_path)

    fresh = ActivityLedger(backup_path, reg)
    live_rows = ledger.query_events(_CONTACT_A)
    backup_rows = fresh.query_events(_CONTACT_A)

    assert len(live_rows) == len(backup_rows) == 2
    for lr, br in zip(live_rows, backup_rows):
        assert lr.event_id == br.event_id
        assert lr.occurred_at == br.occurred_at
        assert lr.state == br.state


def test_export_ledger_completeness(tmp_ledger):
    """export_ledger returns all three tables; override_history rows present; no raw handles."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {"k": "v"})
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    export = ledger.export_ledger()
    assert "contact_events" in export
    assert "approved_overrides" in export
    assert "override_history" in export

    transitions = [row["transition"] for row in export["override_history"]]
    assert "approved" in transitions
    assert "revoked" in transitions

    export_str = json.dumps(export)
    assert _HANDLE_A not in export_str
    assert _HANDLE_B not in export_str


# ===========================================================================
# §7. No event before contact activation
# ===========================================================================


def test_pre_activation_denied_no_grant(tmp_ledger):
    """event_ts one second before activated_at with no grant → zero rows."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert result.decision == "deny_pre_activation"
    assert ledger.query_events(_CONTACT_A) == []


def test_all_deny_decisions_produce_no_row(tmp_path: Path):
    """deny_pre_activation (no grant), deny_paused, deny_retired, review_required → 0 rows."""
    cases = [
        ("deny_pre_activation", None, None),
        ("deny_paused", _CONTACT_A, _LOC),
        ("deny_retired", _CONTACT_A, _LOC),
        ("review_required", None, None),
    ]
    for decision, cid, lid in cases:
        reg = _FakeRegistry()
        reg.set_decision("h", decision, cid, lid)
        ledger = ActivityLedger(tmp_path / f"ledger_{decision}.db", reg)
        result = ledger.record_event("ghl_email", "ev-x", "h", _T_AFTER, {})
        assert result.outcome == "discarded", f"{decision} should discard"
        query_id = cid if cid else "nobody"
        assert ledger.query_events(query_id) == [], f"{decision} should write zero rows"


def test_wrong_location_rejected(tmp_path: Path):
    """resolve_event returning wrong location_id → zero rows, outcome='discarded'."""
    reg = _FakeRegistry()
    reg.set_allow(_HANDLE_A, _CONTACT_A, location_id="wrong-location-xyz")
    ledger = ActivityLedger(tmp_path / "activity.db", reg)
    result = ledger.record_event("ghl_email", "ev-x", _HANDLE_A, _T_AFTER, {})
    assert result.outcome == "discarded"
    assert result.decision == "wrong_location"
    assert ledger.query_events(_CONTACT_A) == []


def test_pre_activation_admitted_with_active_grant(tmp_ledger):
    """event_ts before activated_at with matching active grant → one override_admitted row."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    gr = ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "override_admitted"
    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].state == "override_admitted"
    assert rows[0].override_id == gr.override_id


def test_pre_activation_denied_with_wrong_contact_grant(tmp_ledger):
    """Grant for contact B does not admit pre-activation event that resolves to contact A."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    # Grant is for CONTACT_B, but handle resolves to CONTACT_A
    ledger.grant_override(_CONTACT_B, _LOC, "ghl_email", "ev-pre", "approved")
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(_CONTACT_A) == []


def test_pre_activation_denied_with_revoked_grant(tmp_ledger):
    """A revoked grant does not admit a pre-activation event; zero rows written."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(_CONTACT_A) == []


# ===========================================================================
# §8. Override and Grant Tests
# ===========================================================================


def test_one_event_only_override(tmp_ledger):
    """Grant for source_event_id_A does not admit source_event_id_B for same contact."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-001", "approved")

    r1 = ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_BEFORE, {})
    assert r1.outcome == "override_admitted"

    r2 = ledger.record_event("ghl_email", "ev-002", _HANDLE_A, _T_BEFORE, {})
    assert r2.outcome == "discarded"

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].source_event_id == "ev-001"


def test_wrong_source_grant_rejected(tmp_ledger):
    """Grant for source='ghl_call' does not admit event with source='plaud'."""
    ledger, reg, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_call", "ev-001", "approved")
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    result = ledger.record_event("plaud", "ev-001", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(_CONTACT_A) == []


def test_wrong_location_grant_rejected(tmp_ledger):
    """grant_override with non-Team-Duncan location_id raises ValueError."""
    ledger, _, _ = tmp_ledger
    with pytest.raises(ValueError, match="Team Duncan location"):
        ledger.grant_override(_CONTACT_A, "wrong-loc-xyz", "ghl_email", "ev-pre", "reason")


def test_revoked_grant_no_future_admission(tmp_ledger):
    """After revoke_override, subsequent record_event for same event → zero rows."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    result = ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(_CONTACT_A) == []


def test_revoked_grant_does_not_remove_admitted_rows(tmp_ledger):
    """Rows admitted before revocation retain state='override_admitted' after revoke."""
    ledger, reg, _ = tmp_ledger
    reg.set_deny_pre_activation(_HANDLE_A, _CONTACT_A)
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.record_event("ghl_email", "ev-pre", _HANDLE_A, _T_BEFORE, {})
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 1
    assert rows[0].state == "override_admitted"


def test_revoke_idempotent(tmp_ledger):
    """Calling revoke_override twice → exactly one 'revoked' history row (total: approved+revoked)."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    rv1 = ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    rv2 = ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    assert rv1.outcome == "revoked"
    assert rv2.outcome == "already_revoked"

    export = ledger.export_ledger()
    transitions = [row["transition"] for row in export["override_history"]]
    assert transitions == ["approved", "revoked"]


def test_correction_chain_visible_in_full_history(tmp_ledger):
    """query_full_history shows superseded original and active correction; superseded_by links them."""
    ledger, reg, _ = tmp_ledger
    ledger.record_event("ghl_email", "ev-001", _HANDLE_A, _T_AFTER, {})
    ledger.correct_event("ghl_email", "ev-001", "ev-001c", _HANDLE_A, _T_AFTER, {})

    full = ledger.query_full_history(_CONTACT_A)
    assert len(full) == 2
    old = next(r for r in full if r.source_event_id == "ev-001")
    new = next(r for r in full if r.source_event_id == "ev-001c")
    assert old.state == "superseded"
    assert old.superseded_by == new.event_id
    assert new.state == "active"


def test_override_history_append_only(tmp_ledger):
    """Each grant/revoke appends a row to override_history; no existing row modified."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "approved")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "re-approved")

    export = ledger.export_ledger()
    transitions = [row["transition"] for row in export["override_history"]]
    assert transitions == ["approved", "revoked", "approved"]
    assert len(export["override_history"]) == 3


def test_query_override_history_chronological(tmp_ledger):
    """query_override_history returns transitions in chronological order."""
    ledger, _, _ = tmp_ledger
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "first")
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")
    ledger.grant_override(_CONTACT_A, _LOC, "ghl_email", "ev-pre", "second")

    history = ledger.query_override_history(_CONTACT_A)
    assert len(history) == 1
    transitions = [h["transition"] for h in history[0].history]
    assert transitions == ["approved", "revoked", "approved"]


# ===========================================================================
# §9. Registry Integration Tests
# (use a real ContactRegistry, not a stub)
# ===========================================================================


def _make_real_registry_with_contact(
    tmp_path: Path,
    activated_at: datetime,
    phone: str = "+15551110001",
    contact_id: str = "real-contact-001",
    first_name: str = "Real",
    last_name: str = "Contact",
):
    """Create a real ContactRegistry with one activated contact.

    Returns (registry, contact_id, phone).
    """
    from plugins.team_duncan_contacts.registry import ContactRegistry
    from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)

    contact = {
        "id": contact_id,
        "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": first_name,
        "lastName": last_name,
        "phone": phone,
    }

    clock_ref = [activated_at]

    def _clock():
        return clock_ref[0]

    registry = ContactRegistry(
        hermes_home,
        team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID,
        clock=_clock,
    )

    reader = FakeGhlReader([contact])
    name_query = f"{first_name} {last_name}".strip()
    prep = registry.prepare_activation(name_query, reader)
    assert prep.status == "ready_for_confirmation", f"prep failed: {prep.status}"
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated", f"confirm failed: {confirm.status}"

    return registry, contact_id, phone


def test_registry_integration_before_activation(tmp_path: Path):
    """Real registry: event_ts = activated_at - 1s → deny_pre_activation → zero rows."""
    T = _T_ACTIVATED
    registry, contact_id, phone = _make_real_registry_with_contact(tmp_path, T)

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    t_before = T - timedelta(seconds=1)
    result = ledger.record_event("ghl_email", "ev-001", phone, t_before, {})
    assert result.outcome == "discarded"
    assert result.decision == "deny_pre_activation"
    assert ledger.query_events(contact_id) == []


def test_registry_integration_at_activation(tmp_path: Path):
    """Real registry: event_ts exactly == activated_at → allow → one active row."""
    T = _T_ACTIVATED
    registry, contact_id, phone = _make_real_registry_with_contact(tmp_path, T)

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    result = ledger.record_event("ghl_email", "ev-001", phone, T, {})
    assert result.outcome == "admitted"
    rows = ledger.query_events(contact_id)
    assert len(rows) == 1
    assert rows[0].state == "active"


def test_registry_integration_after_activation(tmp_path: Path):
    """Real registry: event_ts = activated_at + 1s → allow → one active row."""
    T = _T_ACTIVATED
    registry, contact_id, phone = _make_real_registry_with_contact(tmp_path, T)

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    t_after = T + timedelta(seconds=1)
    result = ledger.record_event("ghl_email", "ev-001", phone, t_after, {})
    assert result.outcome == "admitted"
    rows = ledger.query_events(contact_id)
    assert len(rows) == 1
    assert rows[0].state == "active"


def test_registry_integration_one_event_override(tmp_path: Path):
    """Real registry: grant for ev-001 admits it before activation; ev-002 is discarded."""
    T = _T_ACTIVATED
    registry, contact_id, phone = _make_real_registry_with_contact(tmp_path, T)

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    t_before = T - timedelta(hours=1)

    ledger.grant_override(contact_id, _LOC, "ghl_email", "ev-001", "approved")

    r1 = ledger.record_event("ghl_email", "ev-001", phone, t_before, {})
    assert r1.outcome == "override_admitted"

    r2 = ledger.record_event("ghl_email", "ev-002", phone, t_before, {})
    assert r2.outcome == "discarded"

    rows = ledger.query_events(contact_id)
    assert len(rows) == 1
    assert rows[0].source_event_id == "ev-001"
    assert rows[0].state == "override_admitted"


def test_registry_integration_overlapping_replay(tmp_path: Path):
    """Real registry: overlapping ingest runs produce no duplicates;
    pre-activation events without grants produce zero rows;
    post-activation events produce exactly one row each.
    """
    T = _T_ACTIVATED
    registry, contact_id, phone = _make_real_registry_with_contact(tmp_path, T)

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    t_pre = T - timedelta(hours=2)
    t_post = T + timedelta(hours=1)

    # Batch A
    ledger.record_event("ghl_email", "ev-pre-1", phone, t_pre, {})   # no grant → discarded
    ledger.record_event("ghl_email", "ev-post-1", phone, t_post, {})  # admitted

    # Batch B (overlapping)
    ledger.record_event("ghl_email", "ev-pre-1", phone, t_pre, {})   # still discarded
    ledger.record_event("ghl_email", "ev-post-1", phone, t_post, {}) # idempotent
    ledger.record_event("ghl_email", "ev-post-2", phone, t_post, {}) # new admission

    rows = ledger.query_events(contact_id)
    assert len(rows) == 2
    source_eids = {r.source_event_id for r in rows}
    assert "ev-post-1" in source_eids
    assert "ev-post-2" in source_eids
    assert "ev-pre-1" not in source_eids


def test_registry_integration_wrong_contact_grant_rejection(tmp_path: Path):
    """Real registry: grant for contact A does not admit pre-activation event for contact B."""
    from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader

    T = _T_ACTIVATED
    registry, contact_id_a, phone_a = _make_real_registry_with_contact(
        tmp_path, T,
        phone="+15551110001",
        contact_id="contact-a-001",
        first_name="Contact",
        last_name="Alpha",
    )

    # Add contact B to the same registry
    phone_b = "+15552220002"
    contact_b = {
        "id": "contact-b-002",
        "locationId": TEAM_DUNCAN_LOCATION_ID,
        "firstName": "Contact",
        "lastName": "Beta",
        "phone": phone_b,
    }
    reader_b = FakeGhlReader([contact_b])
    prep_b = registry.prepare_activation("Contact Beta", reader_b)
    assert prep_b.status == "ready_for_confirmation"
    confirm_b = registry.confirm_activation(prep_b.token)
    assert confirm_b.status == "activated"
    contact_id_b = "contact-b-002"

    ledger = ActivityLedger(tmp_path / "activity.db", registry)
    t_before = T - timedelta(hours=1)

    # Grant is for contact A
    ledger.grant_override(contact_id_a, _LOC, "ghl_email", "ev-pre", "approved")

    # Event resolves to contact B (phone_b) → grant for A does not apply
    result = ledger.record_event("ghl_email", "ev-pre", phone_b, t_before, {})
    assert result.outcome == "discarded"
    assert ledger.query_events(contact_id_b) == []


# ===========================================================================
# §10. Privacy Canary Tests
# ===========================================================================


def test_privacy_canary_contact_events(tmp_path: Path):
    """Canary raw phone used as raw_handle is absent from all text columns in contact_events."""
    canary = "+15550001111"
    reg = _FakeRegistry()
    reg.set_allow(canary, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)

    for i in range(3):
        t = datetime(2026, 6, 1, i, 0, 0, tzinfo=timezone.utc)
        ledger.record_event("ghl_email", f"ev-{i}", canary, t, {"idx": str(i)})

    rows = ledger.query_events(_CONTACT_A)
    assert len(rows) == 3
    for row in rows:
        for val in [
            row.event_id, row.contact_id, row.location_id, row.source,
            row.source_event_id, row.occurred_at, row.ingested_at, row.state,
            str(row.superseded_by or ""), str(row.override_id or ""),
            row.provenance_json,
        ]:
            assert canary not in val, f"Canary found in column: {val!r}"


def test_privacy_canary_approved_overrides(tmp_path: Path):
    """After creating and revoking a grant, no raw phone appears in approved_overrides."""
    canary_phone = "555-000-9999"
    reg = _FakeRegistry()
    ledger = ActivityLedger(tmp_path / "activity.db", reg)

    ledger.grant_override(
        _CONTACT_A, _LOC, "ghl_email", "ev-pre",
        f"approved: {canary_phone}"
    )
    ledger.revoke_override(_CONTACT_A, "ghl_email", "ev-pre")

    export = ledger.export_ledger()
    for row in export["approved_overrides"]:
        for val in row.values():
            if isinstance(val, str):
                assert canary_phone not in val, f"Canary phone found: {val!r}"


def test_privacy_canary_export(tmp_path: Path):
    """export_ledger() serialized to JSON contains no canary raw handle passed to record_event
    or as approval_reason before sanitization.
    """
    canary_handle = "+15550009876"
    canary_reason_phone = "555-876-5432"
    reg = _FakeRegistry()
    reg.set_allow(canary_handle, _CONTACT_A)
    ledger = ActivityLedger(tmp_path / "activity.db", reg)

    ledger.record_event("ghl_email", "ev-001", canary_handle, _T_AFTER, {"ref": "data"})
    ledger.grant_override(
        _CONTACT_A, _LOC, "ghl_email", "ev-pre",
        f"approved: {canary_reason_phone}"
    )

    export = ledger.export_ledger()
    export_str = json.dumps(export)
    assert canary_handle not in export_str
    assert canary_reason_phone not in export_str
