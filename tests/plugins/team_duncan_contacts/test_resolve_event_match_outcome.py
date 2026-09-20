"""Tests for the OPS-18 additive registry contract on resolve_event().

All tests use fakes and temp directories. No live GHL calls, no real
credentials, no live profile data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.ghl_reader import FakeGhlReader
from plugins.team_duncan_contacts.registry import (
    MATCH_OUTCOME_MULTIPLE,
    MATCH_OUTCOME_UNAVAILABLE,
    MATCH_OUTCOME_UNIQUE,
    MATCH_OUTCOME_ZERO,
    ContactRegistry,
    build_candidate_display_label,
)

LOCATION_ID = "loc-team-duncan-123"
CANARY_PHONE = "+15551239999"


def _utc(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def _activate(registry: ContactRegistry, reader: FakeGhlReader, name_or_id: str) -> str:
    prep = registry.prepare_activation(name_or_id, reader)
    assert prep.status == "ready_for_confirmation", prep.message
    confirm = registry.confirm_activation(prep.token)
    assert confirm.status == "activated"
    return confirm.contact_id


@pytest.fixture()
def registry(tmp_path: Path) -> ContactRegistry:
    return ContactRegistry(tmp_path, team_duncan_location_id=LOCATION_ID)


def test_zero_match_outcome(registry: ContactRegistry) -> None:
    reader = FakeGhlReader([{
        "id": "c1", "locationId": LOCATION_ID, "firstName": "Alice", "lastName": "A",
        "phone": "+15550001111",
    }])
    _activate(registry, reader, "c1")

    result = registry.resolve_event("+15559998888", _utc("2026-01-01T12:00:00"))
    assert result.match_outcome == MATCH_OUTCOME_ZERO
    assert result.decision == "review_required"
    assert result.candidate_rows is None
    assert result.candidate_collision is False


def test_unavailable_when_registry_empty(registry: ContactRegistry) -> None:
    result = registry.resolve_event("+15559998888", _utc("2026-01-01T12:00:00"))
    assert result.match_outcome == MATCH_OUTCOME_UNAVAILABLE
    assert result.decision == "review_required"


def test_unique_match_covers_allow_and_denies(tmp_path: Path) -> None:
    # Injected, fixed activation clock (2026-01-01) sits strictly between the
    # pre-cutoff event (2025-01-01) and the allow-case event (2026-06-01), so
    # the allow assertion holds regardless of wall-clock "now" on test day.
    activation_ts = _utc("2026-01-01T00:00:00")
    registry = ContactRegistry(
        tmp_path, team_duncan_location_id=LOCATION_ID, clock=lambda: activation_ts
    )
    reader = FakeGhlReader([{
        "id": "c1", "locationId": LOCATION_ID, "firstName": "Alice", "lastName": "A",
        "phone": CANARY_PHONE,
    }])
    contact_id = _activate(registry, reader, "c1")

    allowed = registry.resolve_event(CANARY_PHONE, _utc("2026-06-01T12:00:00"))
    assert allowed.match_outcome == MATCH_OUTCOME_UNIQUE
    assert allowed.decision == "allow"
    assert allowed.ghl_contact_id == contact_id

    before_cutoff = registry.resolve_event(CANARY_PHONE, _utc("2025-01-01T00:00:00"))
    assert before_cutoff.match_outcome == MATCH_OUTCOME_UNIQUE
    assert before_cutoff.decision == "deny_pre_activation"

    registry.pause_contact(contact_id, actor="clay")
    paused = registry.resolve_event(CANARY_PHONE, _utc("2026-06-01T12:00:00"))
    assert paused.match_outcome == MATCH_OUTCOME_UNIQUE
    assert paused.decision == "deny_paused"

    registry.retire_contact(contact_id, actor="clay")
    retired = registry.resolve_event(CANARY_PHONE, _utc("2026-06-01T12:00:00"))
    assert retired.match_outcome == MATCH_OUTCOME_UNIQUE
    assert retired.decision == "deny_retired"


def test_multiple_match_candidate_rows_and_selection_tokens(registry: ContactRegistry) -> None:
    reader = FakeGhlReader([
        {"id": "c1", "locationId": LOCATION_ID, "firstName": "Alice", "lastName": "A",
         "phone": "+15550001111"},
        {"id": "c2", "locationId": LOCATION_ID, "firstName": "Bob", "lastName": "B",
         "phone": "+15550002222"},
    ])
    c1 = _activate(registry, reader, "c1")
    c2 = _activate(registry, reader, "c2")

    # Force both contacts to match the same raw handle by re-writing state
    # so they share an HMAC index -- the only way to reach multiple_match
    # without a second GHL contact sharing a phone at prepare time.
    import json

    state_path = tmp_state_path(registry)
    state = json.loads(state_path.read_text())
    shared_hmac = state["contacts"][c1]["hmac_indexes"]["phone"]
    state["contacts"][c2]["hmac_indexes"]["phone"] = shared_hmac
    state_path.write_text(json.dumps(state))

    result = registry.resolve_event(
        "+15550001111", _utc("2026-06-01T12:00:00"),
        source="plaud", source_event_id="evt-1",
    )
    assert result.match_outcome == MATCH_OUTCOME_MULTIPLE
    assert result.decision == "review_required"
    assert result.candidate_collision is False
    assert result.candidate_rows is not None
    assert len(result.candidate_rows) == 2
    for row in result.candidate_rows:
        assert set(row.keys()) == {"selection_token", "display_label"}
        assert row["selection_token"] is not None
        # Non-PII: raw phone digits never leak into the rendered label.
        assert "0001111" not in row["display_label"]
        assert "0002222" not in row["display_label"]

    # Re-derivation is deterministic given the same registry state.
    again = registry.resolve_event(
        "+15550001111", _utc("2026-06-01T12:00:00"),
        source="plaud", source_event_id="evt-1",
    )
    tokens_first = sorted(r["selection_token"] for r in result.candidate_rows)
    tokens_second = sorted(r["selection_token"] for r in again.candidate_rows)
    assert tokens_first == tokens_second

    # A different source_event_id yields different tokens (domain separated
    # per-event, not just per-contact).
    different_event = registry.resolve_event(
        "+15550001111", _utc("2026-06-01T12:00:00"),
        source="plaud", source_event_id="evt-2",
    )
    tokens_third = sorted(r["selection_token"] for r in different_event.candidate_rows)
    assert tokens_third != tokens_first

    # Exact selection resolves to exactly one contact_id.
    winning_token = result.candidate_rows[0]["selection_token"]
    selected = registry.resolve_event(
        "+15550001111", _utc("2026-06-01T12:00:00"),
        source="plaud", source_event_id="evt-1", selection_token=winning_token,
    )
    assert selected.selected_contact_id in (c1, c2)

    wrong_token = "0" * 16
    not_selected = registry.resolve_event(
        "+15550001111", _utc("2026-06-01T12:00:00"),
        source="plaud", source_event_id="evt-1", selection_token=wrong_token,
    )
    assert not_selected.selected_contact_id is None


def test_collision_withholds_all_candidates_and_fails_closed(registry: ContactRegistry) -> None:
    # Two distinct GHL contacts sharing the exact same raw phone, display
    # name, and no email/brokerage/owner on either: a genuine collision.
    # Both contacts' hmac_indexes and masked_labels are computed from this
    # same shared raw phone, so the label and the match are both real --
    # no state hack needed.
    shared_phone = "+15550001111"
    reader = FakeGhlReader([
        {"id": "c1", "locationId": LOCATION_ID, "firstName": "Sam", "lastName": "Q",
         "phone": shared_phone},
        {"id": "c2", "locationId": LOCATION_ID, "firstName": "Sam", "lastName": "Q",
         "phone": shared_phone},
    ])
    _activate(registry, reader, "c1")
    _activate(registry, reader, "c2")

    result = registry.resolve_event(
        shared_phone, _utc("2026-06-01T12:00:00"),
        source="desk_call", source_event_id="evt-collide",
    )
    assert result.match_outcome == MATCH_OUTCOME_MULTIPLE
    assert result.candidate_collision is True
    assert result.candidate_rows is None
    assert result.decision == "review_required"


def test_display_label_sanitizes_and_truncates() -> None:
    contact = {
        "display_name": "A\tB\nC  " + ("x" * 100),
        "masked_labels": {"phone": "***-***-1234"},
        "candidate_label_fields": {
            "masked_email": "j***@example.com",
            "brokerage": "Acme.Realty!",
            "assigned_owner": "Clay Duncan",
        },
    }
    label = build_candidate_display_label(contact)
    parts = label.split("|")
    assert len(parts) == 5
    assert len(parts[0]) <= 60
    assert "\t" not in parts[0] and "\n" not in parts[0]
    assert parts[1] == "****1234"
    assert parts[2] == "j***@example.com"
    assert "\\." in parts[3] and "\\!" in parts[3]
    assert parts[4] == "Clay Duncan"


def test_display_label_absent_fields_render_none() -> None:
    contact = {"display_name": None, "masked_labels": {}, "candidate_label_fields": {}}
    label = build_candidate_display_label(contact)
    assert label == "(none)|(none)|(none)|(none)|(none)"


def test_assigned_owner_mismatch_normalizes_to_none_not_withheld() -> None:
    contact = {
        "display_name": "Pat Taylor",
        "masked_labels": {},
        "candidate_label_fields": {"assigned_owner": None},
    }
    label = build_candidate_display_label(contact)
    assert label.endswith("|(none)")


def test_normalize_owner_allowlist() -> None:
    from plugins.team_duncan_contacts.registry import _normalize_owner

    assert _normalize_owner("Clay Duncan") == "Clay Duncan"
    assert _normalize_owner("Levi Duncan") == "Levi Duncan"
    assert _normalize_owner("  Clay   Duncan  ") == "Clay Duncan"
    assert _normalize_owner("Someone Else") is None
    assert _normalize_owner(None) is None
    assert _normalize_owner("") is None


def tmp_state_path(registry: ContactRegistry) -> Path:
    return registry._state_path  # test-only reach-in, mirrors existing test file's style
