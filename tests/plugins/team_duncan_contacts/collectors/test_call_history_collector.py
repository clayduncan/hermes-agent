"""Tests for the Desk CallHistory collector.

Command construction is pure and fully unit-testable without a network.
All row-fetching in these tests goes through an in-memory fake transport --
no ssh, no sandbox-exec, no real Desk. compute_desk_source_event_id is
covered for stability and non-reversibility.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from plugins.team_duncan_contacts.collectors import call_history_collector as chc_module
from plugins.team_duncan_contacts.collectors.call_history_collector import (
    AmbiguousReplayError,
    CALL_HISTORY_LOOKBACK_DAYS,
    CallHistoryCollector,
    DeskTransportError,
    LiveDeskTransport,
    SANDBOX_FILE_READ_ALLOWLIST,
    _sandbox_profile,
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
    assert argv[0] == "/usr/bin/ssh"
    joined = " ".join(argv)
    assert "/usr/bin/sandbox-exec" in joined
    assert "-readonly" in joined
    assert "mode=ro" in joined
    assert "/usr/bin/sqlite3" in joined
    for path in SANDBOX_FILE_READ_ALLOWLIST:
        assert path in joined
    assert len(SANDBOX_FILE_READ_ALLOWLIST) == 4
    assert "deny default" in joined
    assert "deny file-write" in joined
    assert "deny network" in joined
    # Dynamic value bound via .parameter set, never concatenated into SQL text.
    assert b".parameter set :cutoff_apple_epoch" in stdin
    assert str(800000000.0).split(".")[0] in stdin.decode()


def test_routine_and_replay_use_absolute_ssh_and_sandbox_exec() -> None:
    argv_routine, _ = build_routine_command(
        ssh_identity="i", ssh_target="t", cutoff_apple_epoch=1.0,
    )
    argv_replay, _ = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=1.0,
        zoriginated=1, zanswered=1, duration_s=90,
    )
    for argv in (argv_routine, argv_replay):
        assert argv[0] == "/usr/bin/ssh"
        joined = " ".join(argv)
        assert "/usr/bin/sandbox-exec" in joined


def test_routine_and_replay_omit_unsupported_uri_flag() -> None:
    argv_routine, _ = build_routine_command(
        ssh_identity="i", ssh_target="t", cutoff_apple_epoch=800000000.0,
    )
    argv_replay, _ = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=1.0,
        zoriginated=1, zanswered=1, duration_s=90,
    )
    for argv in (argv_routine, argv_replay):
        joined = " ".join(argv)
        assert "-uri" not in joined.split()
        assert "-readonly" in joined
        assert "-batch" in joined


def test_sandbox_profile_has_exactly_one_root_bootstrap_literal_rule() -> None:
    profile = _sandbox_profile()
    assert profile.count('(allow file-read-data (literal "/"))') == 1
    assert "dyld_shared_cache" not in profile
    assert "Cryptex" not in profile
    assert "cryptex" not in profile
    # Root bootstrap is a literal-only data-read rule, never subpath or regex.
    assert '(literal "/")' in profile
    assert "(subpath" not in profile
    assert "(regex" not in profile


def test_sandbox_profile_denies_writes_and_network_and_only_execs_sqlite3() -> None:
    profile = _sandbox_profile()
    assert "(deny default)" in profile
    assert "(deny file-write*)" in profile
    assert "(deny network*)" in profile
    assert profile.count("(allow process-exec*") == 1
    assert '(allow process-exec* (literal "/usr/bin/sqlite3"))' in profile


def test_sandbox_profile_and_scan_command_contain_no_integrity_only_binaries() -> None:
    profile = _sandbox_profile()
    argv, _stdin = build_routine_command(
        ssh_identity="i", ssh_target="t", cutoff_apple_epoch=1.0,
    )
    joined_argv = " ".join(argv)
    for banned in ("shasum", "perl", "Perl", "/usr/bin/stat", "/usr/bin/openssl"):
        assert banned not in profile
        assert banned not in joined_argv


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


# --- ZDATE tolerance band: recovers numeric drift, never widens matching ----
#
# Real-proof scenario (OPS-110 live-proof correction): SQLite stores
# ZDATE=811365870.02960682; converting that to an ISO-microsecond timestamp
# and back for a replay request yields ~811365870.029607. Exact equality
# defeated the re-fetch even though the expected immutable source-event id
# was separately verified after candidate reconstruction. These tests run
# the actual `.sql` files (read from disk, same as production) through
# Python's sqlite3 module against a synthetic in-memory ZCALLRECORD table --
# a test fixture, not a Desk path, so this does not conflict with the
# production rule against Python's sqlite3 module touching the real Desk.

_STORED_ZDATE = 811365870.02960682
_REQUESTED_ZDATE = 811365870.029607  # ISO-microsecond round-trip drift


def _make_call_history_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ZCALLRECORD ("
        "ZDATE REAL, ZADDRESS TEXT, ZDURATION REAL, "
        "ZORIGINATED INTEGER, ZANSWERED INTEGER)"
    )
    return conn


def _insert_call(
    conn: sqlite3.Connection,
    zdate: float,
    *,
    zaddress: str = CANARY_PHONE,
    zduration: float = 90,
    zoriginated: int = 1,
    zanswered: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO ZCALLRECORD (ZDATE, ZADDRESS, ZDURATION, ZORIGINATED, ZANSWERED) "
        "VALUES (?, ?, ?, ?, ?)",
        (zdate, zaddress, zduration, zoriginated, zanswered),
    )
    conn.commit()


def _run_replay_sql(
    conn: sqlite3.Connection,
    sql_path,
    *,
    zdate_low: float,
    zdate_high: float,
    zoriginated: int = 1,
    zanswered: int = 1,
    duration_low: float | None = None,
    duration_high: float | None = None,
) -> list[dict]:
    params: dict[str, float | int] = {
        "zdate_low": zdate_low, "zdate_high": zdate_high,
        "zoriginated": zoriginated, "zanswered": zanswered,
    }
    if duration_low is not None:
        params["duration_low"] = duration_low
        params["duration_high"] = duration_high
    cur = conn.execute(sql_path.read_text(encoding="utf-8"), params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def test_replay_command_binds_zdate_tolerance_band_not_exact_equality() -> None:
    argv, stdin = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=_REQUESTED_ZDATE,
        zoriginated=1, zanswered=1, duration_s=90,
    )
    text = stdin.decode()
    assert "ZDATE BETWEEN" in text
    assert "ZDATE = :target_zdate" not in text
    assert ":target_zdate" not in text
    assert ".parameter set :zdate_low" in text
    assert ".parameter set :zdate_high" in text


def test_replay_command_zdate_bounds_are_target_plus_minus_one_millisecond() -> None:
    _argv, stdin = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=_REQUESTED_ZDATE,
        zoriginated=1, zanswered=1, duration_s=None,
    )
    text = stdin.decode()
    low_line = next(l for l in text.splitlines() if l.startswith(".parameter set :zdate_low"))
    high_line = next(l for l in text.splitlines() if l.startswith(".parameter set :zdate_high"))
    zdate_low = float(low_line.rsplit(" ", 1)[1])
    zdate_high = float(high_line.rsplit(" ", 1)[1])
    assert zdate_low == pytest.approx(_REQUESTED_ZDATE - 0.001, abs=1e-9)
    assert zdate_high == pytest.approx(_REQUESTED_ZDATE + 0.001, abs=1e-9)


def test_zdate_tolerance_recovers_stored_vs_iso_roundtrip_drift() -> None:
    """Reproduces the live-proof scenario exactly: stored 811365870.02960682
    vs a requested/replayed 811365870.029607 after an ISO-microsecond
    round trip. Exact equality would return zero rows; the tolerance band
    recovers the match."""
    assert _STORED_ZDATE != _REQUESTED_ZDATE
    assert abs(_STORED_ZDATE - _REQUESTED_ZDATE) < 0.001

    conn = _make_call_history_db()
    _insert_call(conn, _STORED_ZDATE, zduration=90)
    rows = _run_replay_sql(
        conn, chc_module._REPLAY_WITH_DURATION_SQL_PATH,
        zdate_low=_REQUESTED_ZDATE - 0.001, zdate_high=_REQUESTED_ZDATE + 0.001,
        duration_low=88, duration_high=92,
    )
    assert len(rows) == 1
    assert rows[0]["ZDATE"] == pytest.approx(_STORED_ZDATE)


def test_zdate_tolerance_recovers_drift_for_no_duration_variant() -> None:
    conn = _make_call_history_db()
    _insert_call(conn, _STORED_ZDATE, zoriginated=0, zanswered=0)
    rows = _run_replay_sql(
        conn, chc_module._REPLAY_NO_DURATION_SQL_PATH,
        zdate_low=_REQUESTED_ZDATE - 0.001, zdate_high=_REQUESTED_ZDATE + 0.001,
        zoriginated=0, zanswered=0,
    )
    assert len(rows) == 1


def test_zdate_tolerance_boundaries_are_inclusive() -> None:
    target = 811365870.0
    tol = 0.001
    conn = _make_call_history_db()
    _insert_call(conn, target - tol, zaddress="+15550000001", zduration=90)
    _insert_call(conn, target + tol, zaddress="+15550000002", zduration=90)
    rows = _run_replay_sql(
        conn, chc_module._REPLAY_WITH_DURATION_SQL_PATH,
        zdate_low=target - tol, zdate_high=target + tol,
        duration_low=88, duration_high=92,
    )
    assert {r["ZADDRESS"] for r in rows} == {"+15550000001", "+15550000002"}


def test_zdate_outside_tolerance_band_is_rejected() -> None:
    target = 811365870.0
    tol = 0.001
    conn = _make_call_history_db()
    # Just outside the band on both sides.
    _insert_call(conn, target - tol - 0.0005, zaddress="+15550000003", zduration=90)
    _insert_call(conn, target + tol + 0.0005, zaddress="+15550000004", zduration=90)
    rows = _run_replay_sql(
        conn, chc_module._REPLAY_WITH_DURATION_SQL_PATH,
        zdate_low=target - tol, zdate_high=target + tol,
        duration_low=88, duration_high=92,
    )
    assert rows == []


def test_zdate_tolerance_keeps_direction_answered_and_duration_filters() -> None:
    """The widened ZDATE band must not loosen the other filters."""
    target = 811365870.0
    tol = 0.001
    conn = _make_call_history_db()
    _insert_call(conn, target, zaddress="+15550000005", zduration=90, zoriginated=0, zanswered=1)
    rows = _run_replay_sql(
        conn, chc_module._REPLAY_WITH_DURATION_SQL_PATH,
        zdate_low=target - tol, zdate_high=target + tol,
        zoriginated=1, zanswered=1,  # wrong direction for the inserted row
        duration_low=88, duration_high=92,
    )
    assert rows == []

    rows = _run_replay_sql(
        conn, chc_module._REPLAY_WITH_DURATION_SQL_PATH,
        zdate_low=target - tol, zdate_high=target + tol,
        zoriginated=0, zanswered=1,
        duration_low=200, duration_high=204,  # outside duration tolerance
    )
    assert rows == []


def test_fetch_exact_event_ambiguous_within_zdate_tolerance_band_fails_closed() -> None:
    """Even when the drifted target_zdate is recovered by the tolerance
    band, two indistinguishable replay candidates must still fail closed --
    the tolerance only recovers numeric representation drift; it never
    selects among multiple candidates."""
    row_a = _row(zdate=_STORED_ZDATE, zaddress=CANARY_PHONE, zduration=90)
    row_b = dict(row_a)
    expected_id = compute_desk_source_event_id(IDENTITY_KEY, _STORED_ZDATE, CANARY_PHONE, 90)

    transport = FakeDeskTransport([], replay_rows=[row_a, row_b])
    collector = CallHistoryCollector(transport, IDENTITY_KEY)
    with pytest.raises(AmbiguousReplayError):
        collector.fetch_exact_event(
            target_zdate=_REQUESTED_ZDATE, zoriginated=1, zanswered=1, duration_s=90,
            expected_source_event_id=expected_id,
        )


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


# --- SSH hardening on the command builders ----------------------------------

_SSH_HARDENING_OPTIONS = ("BatchMode=yes", "StrictHostKeyChecking=accept-new", "ConnectTimeout=10")


def test_routine_command_includes_ssh_hardening_exactly_once_and_retains_identity_target() -> None:
    argv, _stdin = build_routine_command(
        ssh_identity="/Users/claysystemshq/.ssh/desk_deploy",
        ssh_target="clayduncan@100.115.84.20",
        cutoff_apple_epoch=800000000.0,
    )
    joined = " ".join(argv)
    for option in _SSH_HARDENING_OPTIONS:
        assert argv.count(option) == 1
        assert joined.count(option) == 1
    assert "/Users/claysystemshq/.ssh/desk_deploy" in argv
    assert "clayduncan@100.115.84.20" in argv


def test_replay_command_includes_ssh_hardening_exactly_once_and_retains_identity_target() -> None:
    argv, _stdin = build_replay_command(
        ssh_identity="/Users/claysystemshq/.ssh/desk_deploy",
        ssh_target="clayduncan@100.115.84.20",
        target_zdate=1.0, zoriginated=1, zanswered=1, duration_s=90,
    )
    joined = " ".join(argv)
    for option in _SSH_HARDENING_OPTIONS:
        assert argv.count(option) == 1
        assert joined.count(option) == 1
    assert "/Users/claysystemshq/.ssh/desk_deploy" in argv
    assert "clayduncan@100.115.84.20" in argv


# --- .mode json stdin ordering ------------------------------------------------

def test_routine_stdin_begins_with_mode_json_then_params_then_sql() -> None:
    _argv, stdin = build_routine_command(
        ssh_identity="i", ssh_target="t", cutoff_apple_epoch=800000000.0,
    )
    text = stdin.decode()
    mode_idx = text.index(".mode json")
    param_idx = text.index(".parameter set :cutoff_apple_epoch")
    sql_idx = text.index("SELECT ZDATE")
    assert mode_idx < param_idx < sql_idx
    assert text.startswith(".mode json")


def test_replay_stdin_begins_with_mode_json_and_preserves_parameters() -> None:
    _argv, stdin = build_replay_command(
        ssh_identity="i", ssh_target="t", target_zdate=1.0,
        zoriginated=1, zanswered=1, duration_s=90,
    )
    text = stdin.decode()
    assert text.startswith(".mode json")
    assert ".parameter set :zdate_low" in text
    assert ".parameter set :zdate_high" in text
    assert ".parameter set :duration_low" in text
    assert ".parameter set :duration_high" in text
    assert ".parameter set :zoriginated" in text
    assert ".parameter set :zanswered" in text


# --- LiveDeskTransport: injected runner, never real SSH -----------------------

def _completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)


def test_live_desk_transport_uses_fixed_identity_and_target() -> None:
    captured: dict = {}

    def fake_runner(argv, stdin_bytes):
        captured["argv"] = argv
        captured["stdin"] = stdin_bytes
        return _completed(argv, returncode=0, stdout=b"[]")

    transport = LiveDeskTransport(runner=fake_runner)
    rows = transport.run_routine_scan(datetime.now(timezone.utc))

    assert rows == []
    assert "/Users/claysystemshq/.ssh/desk_deploy" in captured["argv"]
    assert "clayduncan@100.115.84.20" in captured["argv"]
    for option in _SSH_HARDENING_OPTIONS:
        assert captured["argv"].count(option) == 1
    assert captured["stdin"].startswith(b".mode json")


def test_live_desk_transport_run_replay_lookup_uses_injected_runner() -> None:
    captured: dict = {}

    def fake_runner(argv, stdin_bytes):
        captured["argv"] = argv
        return _completed(argv, returncode=0, stdout=b"[]")

    transport = LiveDeskTransport(runner=fake_runner)
    rows = transport.run_replay_lookup(
        target_zdate=1.0, zoriginated=1, zanswered=1, duration_s=None,
    )
    assert rows == []
    assert captured["argv"][0] == "/usr/bin/ssh"


def test_live_desk_transport_valid_json_rows_become_exact_dictionaries() -> None:
    row = _row()
    stdout = json.dumps([row]).encode("utf-8")
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    rows = transport.run_routine_scan(datetime.now(timezone.utc))
    assert rows == [row]


def test_live_desk_transport_empty_successful_output_is_empty_list() -> None:
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, b""))
    assert transport.run_routine_scan(datetime.now(timezone.utc)) == []
    transport_ws = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, b"   \n"))
    assert transport_ws.run_routine_scan(datetime.now(timezone.utc)) == []


def test_live_desk_transport_invalid_json_fails_content_free() -> None:
    secret_stdout = b"not json at all " + CANARY_PHONE.encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, secret_stdout))
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    assert CANARY_PHONE not in str(exc_info.value)
    assert "not json at all" not in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_stdout",
    [
        b'{"not": "a list"}',
        b'"just a string"',
        b"42",
    ],
)
def test_live_desk_transport_non_list_root_fails_content_free(bad_stdout) -> None:
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, bad_stdout))
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    assert bad_stdout.decode() not in str(exc_info.value)


def test_live_desk_transport_non_object_item_fails() -> None:
    stdout = json.dumps(["not-an-object"]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_missing_field_fails() -> None:
    row = _row()
    del row["ZDURATION"]
    stdout = json.dumps([row]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_extra_field_fails() -> None:
    row = _row()
    row["UNEXPECTED"] = "value"
    stdout = json.dumps([row]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_bad_type_fails() -> None:
    row = _row()
    row["ZDATE"] = "not-a-number"
    stdout = json.dumps([row]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_bool_as_number_fails() -> None:
    row = _row()
    row["ZANSWERED"] = True
    stdout = json.dumps([row]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_missing_address_fails() -> None:
    row = _row()
    row["ZADDRESS"] = None
    stdout = json.dumps([row]).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_more_than_10000_rows_fails() -> None:
    rows = [_row(zdate=float(i)) for i in range(10_001)]
    stdout = json.dumps(rows).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    with pytest.raises(DeskTransportError):
        transport.run_routine_scan(datetime.now(timezone.utc))


def test_live_desk_transport_exactly_10000_rows_succeeds() -> None:
    rows = [_row(zdate=float(i)) for i in range(10_000)]
    stdout = json.dumps(rows).encode()
    transport = LiveDeskTransport(runner=lambda argv, stdin: _completed(argv, 0, stdout))
    result = transport.run_routine_scan(datetime.now(timezone.utc))
    assert len(result) == 10_000


# --- LiveDeskTransport: process failure paths never leak content -------------

def test_live_desk_transport_timeout_fails_without_leaking() -> None:
    def timeout_runner(argv, stdin_bytes):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

    transport = LiveDeskTransport(runner=timeout_runner)
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    message = str(exc_info.value)
    assert "ssh" not in message
    assert "/Users/claysystemshq/.ssh/desk_deploy" not in message


def test_live_desk_transport_nonzero_return_code_fails_without_leaking() -> None:
    secret_stderr = b"permission denied for " + CANARY_PHONE.encode()
    transport = LiveDeskTransport(
        runner=lambda argv, stdin: _completed(argv, returncode=1, stdout=b"", stderr=secret_stderr)
    )
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    assert CANARY_PHONE not in str(exc_info.value)
    assert "permission denied" not in str(exc_info.value)


def test_live_desk_transport_negative_return_code_signal_fails_without_leaking() -> None:
    transport = LiveDeskTransport(
        runner=lambda argv, stdin: _completed(argv, returncode=-9, stdout=b"", stderr=b"")
    )
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    assert "-9" not in str(exc_info.value)


def test_live_desk_transport_unexpected_runner_exception_fails_content_free() -> None:
    def broken_runner(argv, stdin_bytes):
        raise OSError("ssh binary not found: " + CANARY_PHONE)

    transport = LiveDeskTransport(runner=broken_runner)
    with pytest.raises(DeskTransportError) as exc_info:
        transport.run_routine_scan(datetime.now(timezone.utc))
    assert CANARY_PHONE not in str(exc_info.value)


# --- shell=True is structurally unreachable -----------------------------------

def test_shell_true_is_structurally_unreachable() -> None:
    source = inspect.getsource(chc_module)
    assert "shell=True" not in source
    assert "shell = True" not in source
