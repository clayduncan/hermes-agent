"""Tests for the OPS-114 Plaud webhook HMAC scheme."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from plugins.team_duncan_contacts.webhook_auth import (
    load_or_create_webhook_secret,
    sign,
    verify_signature,
)


def test_secret_is_created_once_and_reused(tmp_path: Path) -> None:
    first = load_or_create_webhook_secret(tmp_path)
    second = load_or_create_webhook_secret(tmp_path)
    assert first == second
    assert len(first) == 32


def test_secret_file_is_0600(tmp_path: Path) -> None:
    load_or_create_webhook_secret(tmp_path)
    path = tmp_path / "plaud_webhook_hmac_secret"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_valid_signature_in_window_is_accepted() -> None:
    secret = b"\x01" * 32
    body = b'{"plaud_recording_id": "rec-1"}'
    now = 1_800_000_000.0
    ts = str(int(now))
    signature = sign(secret, ts, body)
    assert verify_signature(secret, timestamp=ts, body=body, signature=signature, now=now) is True


def test_wrong_secret_is_rejected() -> None:
    body = b"{}"
    ts = str(int(time.time()))
    signature = sign(b"\x01" * 32, ts, body)
    assert verify_signature(b"\x02" * 32, timestamp=ts, body=body, signature=signature) is False


def test_tampered_body_is_rejected() -> None:
    secret = b"\x01" * 32
    ts = str(int(time.time()))
    signature = sign(secret, ts, b"original")
    assert verify_signature(secret, timestamp=ts, body=b"tampered", signature=signature) is False


def test_missing_signature_is_rejected() -> None:
    secret = b"\x01" * 32
    ts = str(int(time.time()))
    assert verify_signature(secret, timestamp=ts, body=b"{}", signature=None) is False


def test_missing_timestamp_is_rejected() -> None:
    secret = b"\x01" * 32
    signature = sign(secret, "123", b"{}")
    assert verify_signature(secret, timestamp=None, body=b"{}", signature=signature) is False


def test_malformed_timestamp_is_rejected() -> None:
    secret = b"\x01" * 32
    signature = sign(secret, "not-a-number", b"{}")
    assert (
        verify_signature(secret, timestamp="not-a-number", body=b"{}", signature=signature)
        is False
    )


def test_stale_timestamp_outside_replay_window_is_rejected() -> None:
    secret = b"\x01" * 32
    body = b"{}"
    now = 1_800_000_000.0
    old_ts = str(int(now) - 301)
    signature = sign(secret, old_ts, body)
    assert verify_signature(secret, timestamp=old_ts, body=body, signature=signature, now=now) is False


def test_replayed_signature_within_window_still_verifies() -> None:
    """A signature replayed inside the 300s window verifies at the HMAC
    layer -- replay-*within*-window protection is not this layer's job;
    it's process_one()'s plaud_summary_state idempotency that makes an
    in-window replay safe end to end."""
    secret = b"\x01" * 32
    body = b'{"plaud_recording_id": "rec-1"}'
    now = 1_800_000_000.0
    ts = str(int(now))
    signature = sign(secret, ts, body)
    first = verify_signature(secret, timestamp=ts, body=body, signature=signature, now=now)
    second = verify_signature(secret, timestamp=ts, body=body, signature=signature, now=now + 5)
    assert first is True
    assert second is True


def test_timestamp_exactly_at_window_boundary_is_accepted() -> None:
    secret = b"\x01" * 32
    body = b"{}"
    now = 1_800_000_000.0
    ts = str(int(now) - 300)
    signature = sign(secret, ts, body)
    assert verify_signature(secret, timestamp=ts, body=body, signature=signature, now=now) is True
