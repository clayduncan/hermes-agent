"""Tests for the Desk CallHistory collector.

Command construction is pure and fully unit-testable without a network.
All row-fetching in these tests goes through an in-memory fake transport --
no ssh, no sandbox-exec, no real Desk. compute_desk_source_event_id is
covered for stability and non-reversibility.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.team_duncan_contacts.collectors.call_history_collector import (
    AmbiguousReplayError,
    CALL_HISTORY_LOOKBACK_DAYS,
    CallHistoryCollector,
    SANDBOX_FILE_READ_ALLOWLIST,
    apple_epoch_to_utc,
    build_replay_command,
    build_routine_command,
    compute_desk_source_event_id,
    routine_cutoff_apple_epoch,
    utc_to_apple_epoch,
)

IDENTITY_KEY = b"\x01" * 32
CANARY_PHONE = "+15551239999"


class FakeDeskTransport:
    def __init__(self, routine_rows: list[dict], replay_rows: list[dict] | None = None) -> None:
        self._routine_rows = routine_rows
        self._replay_rows = replay_rows if replay_rows is not None else routine_rows
        self.routine_calls = 0
        self.replay_calls: list[dict] = []

    def run_routine_scan(self, now: datetime) -> list[dict]:
        self.routine_calls += 1
        return self._routine_rows

    def run_replay_lookup(self, *, target_zdate, zoriginated, zanswered, duration_s):
        self.replay_calls.append(
            {"target_zdate": target_zdate, "zoriginated": zoriginated,
             "zanswered": zanswered, "duration_s": duration_s}
        )
        return self._replay_rows


def _row(zdate=800000000.0, zaddress=CANARY_PHONE, zduration=90, zoriginated=1, zanswered=1):
    return {
        "ZDATE": zdate, "ZADDRESS": zaddress, "ZDURATION": zduration,
        "ZORIGINATED": zoriginated, "ZANSWERED": zanswered,
    }


# --- Apple epoch math -------------------------------------------------------

def test_apple_epoch_roundtrip() -> None:
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    zdate = utc_to_apple_epoch(dt)
    back = apple_epoch_to_utc(zdate)
    assert back == dt


def test_routine_cutoff_is_fixed_seven_days() -> None:
    assert CALL_HISTORY_LOOKBACK_DAYS == 7
    now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
    cutoff_epoch = routine_cutoff_apple_epoch(now)
    cutoff_dt = apple_epoch_to_utc(cutoff_epoch)
    assert cutoff_dt == now - timedelta(days=7)


# --- Command construction (pure, no execution) ------------------------------

def test_routine_command_uses_fixed_sandbox_and_no_shell_interpolation() -> None:
    argv, stdin = build_routine_command(
        ssh_identity="~/.ssh/desk_deploy", ssh_target="user@host",
        cutoff_apple_epoch=800000000.0,
    )
    assert argv[0] == "ssh"
    joined = " ".join(argv)
    assert "sandbox-exec" in joined
    assert "-readonly" in joined
    assert "mode=ro" in joined
    assert "/usr/bin/sqlite3" in joined
    for path in SANDBOX_FILE_READ_ALLOWLIST:
        assert path in joined
    assert len(SANDBOX_FILE_READ_ALLOWLIST) == 5
    assert "deny default" in joined
    assert "deny file-write" in joined
    assert "deny network" in joined
    # Dynamic value bound via .parameter set, never concatenated into SQL text.
    assert b".parameter set :cutoff_apple_epoch" in stdin
    assert str(800000000.0).split(".")[0] in stdin.decode()


def test_command_construction_rejects_bool_and_str_dynamic_values() -> None:
    with pytest.raises(TypeError):
        build_routine_command(ssh_identity="i", ssh_target="t", cutoff_apple_epoch=True)
    with pytest.raises(TypeError):
        build_routine_command(ssh_identity="i", ssh_target="t", cutoff_apple_epoch="800000000")


def test_replay_command_selects_duration_variant() -> None:
    argv, stdin = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=1.0,
        zoriginated=1, zanswered=1, duration_s=90,
    )
    text = stdin.decode()
    assert ":duration_low" in text
    assert ":duration_high" in text
    assert "ZDURATION BETWEEN" in text


def test_replay_command_no_duration_variant_never_hardcodes_answered() -> None:
    argv, stdin = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=1.0,
        zoriginated=0, zanswered=0, duration_s=None,
    )
    text = stdin.decode()
    assert ":duration_low" not in text
    assert "ZDURATION BETWEEN" not in text
    assert ".parameter set :zanswered 0" in text


# --- source_event_id: stable, non-reversible --------------------------------

def test_desk_source_event_id_is_stable_for_identical_inputs() -> None:
    a = compute_desk_source_event_id(IDENTITY_KEY, 800000000.0, CANARY_PHONE, 90)
    b = compute_desk_source_event_id(IDENTITY_KEY, 800000000.0, CANARY_PHONE, 90)
    assert a == b


def test_desk_source_event_id_differs_for_different_calls() -> None:
    a = compute_desk_source_event_id(IDENTITY_KEY, 800000000.0, CANARY_PHONE, 90)
    b = compute_desk_source_event_id(IDENTITY_KEY, 800000001.0, CANARY_PHONE, 90)
    assert a != b


def test_desk_source_event_id_does_not_reveal_raw_phone() -> None:
    event_id = compute_desk_source_event_id(IDENTITY_KEY, 800000000.0, CANARY_PHONE, 90)
    digits = "".join(c for c in CANARY_PHONE if c.isdigit())
    assert digits not in event_id
    # Not recoverable by brute-forcing the phone number space with a
    # different (unknown) key either -- different key, different id.
    other_key = b"\x02" * 32
    with_other_key = compute_desk_source_event_id(other_key, 800000000.0, CANARY_PHONE, 90)
    assert with_other_key != event_id


# --- Collector: normalization + exact-event point lookup --------------------

def test_fetch_routine_window_normalizes_rows() -> None:
    transport = FakeDeskTransport([_row()])
    collector = CallHistoryCollector(transport, IDENTITY_KEY)
    records = collector.fetch_routine_window(datetime.now(timezone.utc))
    assert len(records) == 1
    rec = records[0]
    assert rec.source == "desk_call"
    assert rec.direction == "outbound"
    assert rec.answered == 1
    assert rec.raw_handle == CANARY_PHONE


def test_fetch_exact_event_discards_non_matching_and_accepts_exact_hash() -> None:
    matching_row = _row(zdate=1.0, zaddress=CANARY_PHONE, zduration=90)
    decoy_row = _row(zdate=1.0, zaddress="+15550000000", zduration=90)
    expected_id = compute_desk_source_event_id(IDENTITY_KEY, 1.0, CANARY_PHONE, 90)

    transport = FakeDeskTransport([], replay_rows=[decoy_row, matching_row])
    collector = CallHistoryCollector(transport, IDENTITY_KEY)
    found = collector.fetch_exact_event(
        target_zdate=1.0, zoriginated=1, zanswered=1, duration_s=90,
        expected_source_event_id=expected_id,
    )
    assert found is not None
    assert found.raw_handle == CANARY_PHONE


def test_fetch_exact_event_zero_matches_returns_none() -> None:
    transport = FakeDeskTransport([], replay_rows=[])
    collector = CallHistoryCollector(transport, IDENTITY_KEY)
    found = collector.fetch_exact_event(
        target_zdate=1.0, zoriginated=1, zanswered=1, duration_s=90,
        expected_source_event_id="does-not-exist",
    )
    assert found is None


def test_fetch_exact_event_ambiguous_raises() -> None:
    row_a = _row(zdate=1.0, zaddress=CANARY_PHONE, zduration=90)
    row_b = dict(row_a)  # identical row appearing twice -> same computed id twice
    expected_id = compute_desk_source_event_id(IDENTITY_KEY, 1.0, CANARY_PHONE, 90)

    transport = FakeDeskTransport([], replay_rows=[row_a, row_b])
    collector = CallHistoryCollector(transport, IDENTITY_KEY)
    with pytest.raises(AmbiguousReplayError):
        collector.fetch_exact_event(
            target_zdate=1.0, zoriginated=1, zanswered=1, duration_s=90,
            expected_source_event_id=expected_id,
        )
