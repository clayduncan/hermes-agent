"""Tests for the OPS-114 Plaud webhook receiver.

`handle_webhook_request` is the receiver's entire decision logic,
independent of any socket -- exercised directly here so these tests run
even in sandboxes that forbid binding a TCP socket at all (this build's
own dev sandbox does). A supplementary real-server round-trip test exists
below and is skipped (not failed) if the environment refuses to bind
127.0.0.1:0, so it still runs for real in an unrestricted CI environment.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from plugins.team_duncan_contacts import automation_runner
from plugins.team_duncan_contacts.plaud_webhook_receiver import (
    DEFAULT_PATH,
    build_server,
    drain_pending_events,
    handle_webhook_request,
)
from plugins.team_duncan_contacts.webhook_auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign,
)
from plugins.team_duncan_contacts.webhook_queue import WebhookQueue

SECRET = b"\x03" * 32


def _signed_headers(body: bytes, *, ts: str | None = None) -> dict[str, str]:
    timestamp = ts or str(int(time.time()))
    return {
        SIGNATURE_HEADER: sign(SECRET, timestamp, body),
        TIMESTAMP_HEADER: timestamp,
    }


@pytest.fixture()
def dispatched() -> list[str]:
    return []


def _dispatch(calls: list[str]):
    def _fn(recording_id: str) -> None:
        calls.append(recording_id)

    return _fn


def _always_enqueue(recording_id: str) -> bool:
    """A stub durable-enqueue that always reports success, for tests that
    are not themselves exercising the queue's durability/dedupe contract."""
    return True


# ---------------------------------------------------------------------------
# Core decision logic -- no socket required.
# ---------------------------------------------------------------------------


def test_valid_signed_event_is_accepted_and_dispatched(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 202
    assert payload == {"status": "accepted"}
    assert dispatched == ["rec-1"]


def test_missing_signature_is_rejected_with_401(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers={}, body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 401
    assert payload["reason"] == "invalid_signature"
    assert dispatched == []


def test_wrong_secret_signature_is_rejected(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    ts = str(int(time.time()))
    headers = {SIGNATURE_HEADER: sign(b"\x99" * 32, ts, body), TIMESTAMP_HEADER: ts}
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=headers, body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 401
    assert dispatched == []


def test_tampered_body_after_signing_is_rejected(dispatched) -> None:
    original = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    headers = _signed_headers(original)
    tampered = json.dumps({"plaud_recording_id": "rec-EVIL"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=headers, body=tampered,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 401
    assert dispatched == []


def test_stale_timestamp_is_rejected(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    stale_ts = str(int(time.time()) - 10_000)
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body, ts=stale_ts), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 401
    assert dispatched == []


def test_missing_recording_id_is_rejected_as_invalid_payload(dispatched) -> None:
    body = json.dumps({"title": "irrelevant"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 400
    assert payload["reason"] == "invalid_payload"
    assert dispatched == []


def test_non_json_body_is_rejected_as_invalid_payload(dispatched) -> None:
    body = b"not json at all"
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 400
    assert payload["reason"] == "invalid_payload"
    assert dispatched == []


def test_transcript_and_title_fields_are_never_dispatched(dispatched) -> None:
    """The receiver must ignore transcript text, titles, speaker names, and
    summaries even when present -- only the recording ID reaches dispatch."""
    body = json.dumps({
        "plaud_recording_id": "rec-1",
        "title": "Call with a secret client name",
        "transcript_text": "this must never leak",
        "speaker_names": ["Alice", "Bob"],
        "summary": "a leaked summary",
    }).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 202
    assert dispatched == ["rec-1"]


def test_unknown_path_is_404(dispatched) -> None:
    body = b"{}"
    status, payload = handle_webhook_request(
        request_path="/not/the/right/path", headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 404
    assert dispatched == []


def test_oversized_body_is_rejected(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "x" * 200_000}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched), max_body_bytes=65536,
    )
    assert status == 400
    assert payload["reason"] == "invalid_body_size"
    assert dispatched == []


def test_replayed_event_within_window_is_accepted_twice(dispatched) -> None:
    """The HTTP layer itself does not deduplicate -- a valid in-window
    replay is accepted and dispatched again; idempotency is
    PlaudSummaryRunner.process_one()'s job (see test_plaud_summary_runner.py
    TestProcessOneWebhookPath.test_replayed_webhook_event_is_idempotent)."""
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    headers = _signed_headers(body)
    first = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=headers, body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    second = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=headers, body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert first[0] == 202
    assert second[0] == 202
    assert dispatched == ["rec-1", "rec-1"]


def test_response_never_contains_recording_id_or_source_content(dispatched) -> None:
    body = json.dumps({"plaud_recording_id": "rec-super-secret-id"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=_always_enqueue, dispatch=_dispatch(dispatched),
    )
    assert status == 202
    assert "rec-super-secret-id" not in json.dumps(payload)
    assert payload == {"status": "accepted"}


def test_enqueue_failure_is_503_never_202(dispatched) -> None:
    """If the durable write itself cannot be made, the receiver must never
    tell the sender "accepted" -- that would be a promise of crash-survival
    this process cannot keep. dispatch must never run either: there is
    nothing durable to dispatch yet."""
    body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET, enqueue=lambda recording_id: False, dispatch=_dispatch(dispatched),
    )
    assert status == 503
    assert payload["status"] == "rejected"
    assert dispatched == []


# ---------------------------------------------------------------------------
# Durable queue: accepted-before-crash replay, dedupe, drain removal.
# ---------------------------------------------------------------------------


def test_accepted_event_survives_a_crash_and_is_replayed_on_drain(
    tmp_path: Path, dispatched
) -> None:
    """The exact defect this build fixes: the receiver returns 202, but the
    process crashes before its background dispatch thread ever runs (here
    simulated with a dispatch stub that does nothing). The event must not
    be lost -- it is still sitting in the durable queue, and
    drain_pending_events() (what this module's own startup calls) finds
    and replays it through the same automation runner, exactly like a
    fresh process recovering after a real crash would."""
    queue = WebhookQueue(tmp_path / "plugin-data" / "team_duncan_contacts")

    def _crashed_dispatch(recording_id: str) -> None:
        pass  # the process died before this would have done anything

    body = json.dumps({"plaud_recording_id": "rec-crash-1"}).encode()
    status, payload = handle_webhook_request(
        request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
        secret=SECRET,
        enqueue=lambda recording_id: queue.enqueue(recording_id, now=time.time()),
        dispatch=_crashed_dispatch,
    )
    assert status == 202
    assert queue.list_pending() == ["rec-crash-1"]

    replayed: list[str] = []

    def _fake_run_fn(argv, *, hermes_home):
        replayed.append(argv[argv.index("--plaud-recording-id") + 1])
        return automation_runner.EXIT_COMPLETED

    drained = drain_pending_events(tmp_path, run_fn=_fake_run_fn)
    assert drained == ["rec-crash-1"]
    assert replayed == ["rec-crash-1"]
    assert queue.list_pending() == []  # successful drain removal


def test_drain_leaves_lock_skipped_or_failed_events_pending_for_retry(tmp_path: Path) -> None:
    """A drain attempt that does not actually complete (a live-owner lock
    collision, or a genuine failure) must leave the event queued -- it is
    not lost, and the next drain attempt (the next receiver startup, or a
    future retry) will see it again."""
    queue = WebhookQueue(tmp_path / "plugin-data" / "team_duncan_contacts")
    queue.enqueue("rec-skip-1", now=time.time())

    def _fake_run_fn(argv, *, hermes_home):
        return automation_runner.EXIT_SKIPPED_LOCK

    drained = drain_pending_events(tmp_path, run_fn=_fake_run_fn)
    assert drained == []
    assert queue.list_pending() == ["rec-skip-1"]


def test_drain_leaves_a_processing_error_result_queued(tmp_path: Path, monkeypatch) -> None:
    """The exact bug this build fixes: automation_runner.run() must not
    report EXIT_COMPLETED for a Plaud summary that had errors > 0, or this
    durable queue row would be removed even though the recording was never
    actually processed. Drives the real automation_runner.run(), not a
    fake run_fn, so this proves the fix at the seam drain_pending_events()
    actually calls."""
    queue = WebhookQueue(tmp_path / "plugin-data" / "team_duncan_contacts")
    queue.enqueue("rec-err-1", now=time.time())

    import plugins.team_duncan_contacts as team_duncan_contacts

    class _ErroringRunner:
        def process_one(self, plaud_recording_id: str):
            from plugins.team_duncan_contacts.plaud_summary_runner import PlaudSummaryRunSummary

            return PlaudSummaryRunSummary(errors=1)

    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader",
        lambda hermes_home: (object(), object(), "loc-1"),
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory",
        lambda hermes_home, registry, ghl_reader: (lambda: (_ErroringRunner(), object())),
    )

    drained = drain_pending_events(tmp_path)
    assert drained == []
    assert queue.list_pending() == ["rec-err-1"]


def test_drain_removes_a_zero_error_result(tmp_path: Path, monkeypatch) -> None:
    queue = WebhookQueue(tmp_path / "plugin-data" / "team_duncan_contacts")
    queue.enqueue("rec-ok-1", now=time.time())

    import plugins.team_duncan_contacts as team_duncan_contacts

    class _CleanRunner:
        def process_one(self, plaud_recording_id: str):
            from plugins.team_duncan_contacts.plaud_summary_runner import PlaudSummaryRunSummary

            return PlaudSummaryRunSummary(matched=1, errors=0)

    monkeypatch.setattr(
        team_duncan_contacts, "build_registry_and_reader",
        lambda hermes_home: (object(), object(), "loc-1"),
    )
    monkeypatch.setattr(
        team_duncan_contacts, "_build_plaud_summary_runner_factory",
        lambda hermes_home, registry, ghl_reader: (lambda: (_CleanRunner(), object())),
    )

    drained = drain_pending_events(tmp_path)
    assert drained == ["rec-ok-1"]
    assert queue.list_pending() == []


def test_replayed_webhook_deliveries_dedupe_in_the_durable_queue(tmp_path: Path) -> None:
    """Two webhook deliveries for the same immutable recording ID must
    result in exactly one durably-queued row, never two."""
    queue = WebhookQueue(tmp_path / "plugin-data" / "team_duncan_contacts")
    enqueue = lambda recording_id: queue.enqueue(recording_id, now=time.time())  # noqa: E731

    body = json.dumps({"plaud_recording_id": "rec-dupe-1"}).encode()
    for _ in range(2):
        status, _payload = handle_webhook_request(
            request_path=DEFAULT_PATH, headers=_signed_headers(body), body=body,
            secret=SECRET, enqueue=enqueue, dispatch=lambda recording_id: None,
        )
        assert status == 202

    assert queue.list_pending() == ["rec-dupe-1"]


# ---------------------------------------------------------------------------
# Real server round trip -- skipped if this environment forbids socket bind.
# ---------------------------------------------------------------------------


def test_real_server_round_trip(tmp_path: Path) -> None:
    calls: list[str] = []
    try:
        server = build_server(
            "127.0.0.1", 0, secret=SECRET, hermes_home=tmp_path, dispatch=_dispatch(calls),
        )
    except PermissionError:
        pytest.skip("this sandbox forbids binding a TCP socket, even loopback")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps({"plaud_recording_id": "rec-1"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("POST", DEFAULT_PATH, body=body, headers=_signed_headers(body))
            resp = conn.getresponse()
            assert resp.status == 202
            assert json.loads(resp.read()) == {"status": "accepted"}
        finally:
            conn.close()
        assert calls == ["rec-1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
