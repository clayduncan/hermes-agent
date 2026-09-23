"""OPS-114 Plaud "Transcript & Summary Ready" webhook receiver.

A narrowly-scoped, standalone Team Duncan artifact -- not a route on the
Gateway's generic ``WebhookAdapter`` (gateway/platforms/webhook.py). That
adapter's only two dispatch modes are ``deliver_only`` (forward a rendered
message to a fixed destination like Telegram) and full agent-mediated
execution (spin up an LLM session); neither can invoke a specific
deterministic runner function without either agent mediation or a broad
core change to add a third dispatch mode. Rather than widen a shared core
seam, this module is its own minimal stdlib ``http.server`` service, using
the exact same HMAC scheme (``X-Webhook-Signature-V2`` /
``X-Webhook-Timestamp``, see webhook_auth.py) so it stays consistent with
the rest of the fleet's webhook auth.

Contract:
  - HMAC required on every request. No insecure/bypass mode exists in this
    module at all -- not even for loopback tests, which present a real
    signature computed with the same secret the receiver holds.
  - The only trusted field from the request body is the immutable Plaud
    recording ID. Titles, transcript text, speaker names, and summaries are
    never read from the payload, even if present -- `normalize_zapier_payload`
    extracts nothing else. Timing/duration in the body is informational
    only and is never passed to the processing path; PlaudSummaryRunner
    .process_one() always does its own sealed `fetch_by_identity` re-fetch.
  - Before any 202, the recording ID is durably enqueued (webhook_queue.py,
    an atomic SQLite insert) under Team Duncan plugin data -- never held
    only in memory. If that durable write itself fails, the receiver
    returns 503, never 202: a response of "accepted" is a promise that the
    event survives a crash of this process, and that promise cannot be
    made until the write is committed. Only the immutable recording ID is
    ever stored there; the same content restrictions as the HTTP payload
    itself apply.
  - Once durably enqueued, the event is also dispatched to
    `automation_runner.run(["plaud-webhook", ...])` on a background thread
    as a best-effort fast path, sharing the exact same OPS-114 durable lock
    every other automated mode uses, and removed from the durable queue
    only once that run actually completes. The HTTP response never waits
    for that run to finish -- 202 means "durably accepted for processing",
    not "completed" -- and never carries counts or any other run detail.
  - If this process crashes before that background thread finishes (or
    before it ever started), the event is not lost: it is still sitting in
    the durable queue. `drain_pending_events()` replays every still-pending
    event through the same automation runner and lock, and is called at
    this module's own startup (`main()`) specifically to recover from that
    exact crash window.
  - Event replay is idempotent at two layers: the queue itself dedupes by
    recording ID (a second webhook delivery for an already-queued
    recording is not a second row), and PlaudSummaryRunner.process_one()
    only ever repeats a transcript fetch / summarizer call / GHL write for
    a recording that is not already in a terminal plaud_summary_state.
  - This module is never started by anything in this build. Running it is
    a separate, Clay-controlled activation step (see the OPS-114 ops doc
    for the exact wiring: Zapier "Transcript & Summary Ready" -> HMAC
    secret -> this receiver's host:port/path).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from .webhook_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify_signature
from .webhook_queue import WebhookQueue

log = logging.getLogger(__name__)

DEFAULT_PATH = "/webhooks/team_duncan/plaud"
DEFAULT_MAX_BODY_BYTES = 65536

# sysexits.h EX_CONFIG: this process is fine, a configuration value is
# just not set to enable it. Distinct from a transient/retryable failure.
EXIT_DISABLED = 78


def _data_dir(hermes_home: Path) -> Path:
    return Path(hermes_home) / "plugin-data" / "team_duncan_contacts"


class InvalidWebhookPayloadError(ValueError):
    pass


def normalize_zapier_payload(payload: dict[str, Any]) -> str:
    """Extract only the immutable Plaud recording ID from a normalized
    Zapier "Transcript & Summary Ready" payload. Every other field --
    title, transcript text, speaker names, summary -- is ignored even if
    present; this function never reads them."""
    if not isinstance(payload, dict):
        raise InvalidWebhookPayloadError("payload is not a JSON object")
    recording_id = payload.get("plaud_recording_id") or payload.get("recording_id")
    if not recording_id or not isinstance(recording_id, str):
        raise InvalidWebhookPayloadError("missing plaud_recording_id")
    return recording_id


def _default_enqueue(hermes_home: Path) -> Callable[[str], bool]:
    queue = WebhookQueue(_data_dir(hermes_home))

    def _enqueue(plaud_recording_id: str) -> bool:
        return queue.enqueue(plaud_recording_id, now=time.time())

    return _enqueue


def _default_dispatch(hermes_home: Path) -> Callable[[str], None]:
    from . import automation_runner

    def _dispatch(plaud_recording_id: str) -> None:
        def _run_and_drain() -> None:
            exit_code = automation_runner.run(
                ["plaud-webhook", "--plaud-recording-id", plaud_recording_id],
                hermes_home=hermes_home,
            )
            if exit_code == automation_runner.EXIT_COMPLETED:
                WebhookQueue(_data_dir(hermes_home)).remove(plaud_recording_id)

        thread = threading.Thread(
            target=_run_and_drain,
            daemon=True,
            name="ops114-plaud-webhook-dispatch",
        )
        thread.start()

    return _dispatch


def drain_pending_events(hermes_home: Path, *, run_fn: Callable[..., int] | None = None) -> list[str]:
    """Replay every still-pending durably-queued event through the same
    automation runner and lock every other automated mode uses. Meant to be
    called once at this module's own process startup, to recover anything
    accepted (202) before a prior crash of this process but never drained
    (the crash window this queue exists to close). Returns the recording
    IDs that were successfully drained (and therefore removed from the
    queue) this call; anything left pending (a lock skip, or a genuine
    failure) stays queued for the next drain attempt.

    *run_fn* defaults to automation_runner.run and is only overridable for
    tests -- production callers never pass it."""
    from . import automation_runner

    run_fn = run_fn or automation_runner.run
    queue = WebhookQueue(_data_dir(hermes_home))
    drained: list[str] = []
    for recording_id in queue.list_pending():
        exit_code = run_fn(
            ["plaud-webhook", "--plaud-recording-id", recording_id], hermes_home=hermes_home,
        )
        if exit_code == automation_runner.EXIT_COMPLETED:
            queue.remove(recording_id)
            drained.append(recording_id)
    return drained


def handle_webhook_request(
    *,
    request_path: str,
    headers: Mapping[str, str],
    body: bytes,
    secret: bytes,
    enqueue: Callable[[str], bool],
    dispatch: Callable[[str], None],
    expected_path: str = DEFAULT_PATH,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> tuple[int, dict[str, Any]]:
    """The receiver's entire decision logic, independent of any socket or
    HTTP server -- the real server (below) is a thin adapter over this, and
    tests drive it directly (no networking required, no sandbox/loopback
    dependency). Returns (http_status, json-safe response body); the
    response body is always safe-status-only, never source content."""
    if request_path != expected_path:
        return 404, {"status": "not_found"}

    if not body or len(body) > max_body_bytes:
        return 400, {"status": "rejected", "reason": "invalid_body_size"}

    signature = headers.get(SIGNATURE_HEADER)
    timestamp = headers.get(TIMESTAMP_HEADER)
    if not verify_signature(secret, timestamp=timestamp, body=body, signature=signature):
        return 401, {"status": "rejected", "reason": "invalid_signature"}

    try:
        payload = json.loads(body)
        recording_id = normalize_zapier_payload(payload)
    except (ValueError, InvalidWebhookPayloadError):
        return 400, {"status": "rejected", "reason": "invalid_payload"}

    # The event must be durably recorded before the sender is ever told
    # "accepted" -- if that write itself fails, this is a hard failure
    # (503), never a 202 that would be a promise this process cannot keep.
    if not enqueue(recording_id):
        return 503, {"status": "rejected", "reason": "enqueue_failed"}

    dispatch(recording_id)
    return 202, {"status": "accepted"}


def _make_handler_class(
    *,
    secret: bytes,
    enqueue: Callable[[str], bool],
    dispatch: Callable[[str], None],
    path: str,
    max_body_bytes: int,
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "TeamDuncanPlaudWebhook/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.info("[plaud_webhook] " + fmt, *args)

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            response_body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = -1
            request_body = self.rfile.read(length) if length > 0 else b""

            status, response_payload = handle_webhook_request(
                request_path=self.path,
                headers=self.headers,
                body=request_body,
                secret=secret,
                enqueue=enqueue,
                dispatch=dispatch,
                expected_path=path,
                max_body_bytes=max_body_bytes,
            )
            self._respond(status, response_payload)

        def do_GET(self) -> None:  # noqa: N802
            self._respond(404, {"status": "not_found"})

    return _Handler


def build_server(
    host: str,
    port: int,
    *,
    secret: bytes,
    hermes_home: Path,
    path: str = DEFAULT_PATH,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    enqueue: Callable[[str], bool] | None = None,
    dispatch: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the receiver's HTTP server. *enqueue*
    defaults to a durable SQLite write (webhook_queue.py) under
    *hermes_home*; *dispatch* defaults to a background-thread call into
    automation_runner.run() for the "plaud-webhook" mode. Tests inject
    stubs for both to assert on accepted recording IDs without touching a
    real queue file or spawning a real automated run."""
    handler_cls = _make_handler_class(
        secret=secret,
        enqueue=enqueue or _default_enqueue(hermes_home),
        dispatch=dispatch or _default_dispatch(hermes_home),
        path=path,
        max_body_bytes=max_body_bytes,
    )
    return ThreadingHTTPServer((host, port), handler_cls)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point. Never invoked by any scheduler or by this
    build; a separate, Clay-controlled activation step starts this
    process. See the OPS-114 ops doc for host/port/path/secret wiring.

    Fails closed on ``plaud_webhook_enabled`` (see
    ``plugins.team_duncan_contacts.is_plaud_webhook_enabled``) before
    binding a socket, creating/loading the HMAC secret, opening/creating
    the webhook queue, or draining events: the default/current OPS-114
    architecture is 15-minute Plaud reconciliation only, and this receiver
    is dormant unless that setting is the literal boolean true."""
    import argparse

    parser = argparse.ArgumentParser(prog="plaud_webhook_receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    from . import is_plaud_webhook_enabled

    if not is_plaud_webhook_enabled():
        log.info(
            "[plaud_webhook] disabled (plaud_webhook_enabled is not true); "
            "exiting before binding a socket or touching any webhook state."
        )
        return EXIT_DISABLED

    from hermes_constants import get_hermes_home

    from .webhook_auth import load_or_create_webhook_secret

    hermes_home = Path(get_hermes_home())
    data_dir = _data_dir(hermes_home)
    secret = load_or_create_webhook_secret(data_dir)

    # Recover from the crash window this queue exists to close: anything
    # still pending from before this process's last exit (accepted with a
    # 202 but never fully drained) gets replayed before we start accepting
    # new events.
    drained = drain_pending_events(hermes_home)
    if drained:
        log.info("[plaud_webhook] drained %d pending event(s) from a prior run", len(drained))

    server = build_server(args.host, args.port, secret=secret, hermes_home=hermes_home, path=args.path)
    log.info("[plaud_webhook] listening on %s:%d%s", args.host, args.port, args.path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())


__all__ = [
    "normalize_zapier_payload",
    "InvalidWebhookPayloadError",
    "handle_webhook_request",
    "drain_pending_events",
    "build_server",
    "main",
    "DEFAULT_PATH",
    "DEFAULT_MAX_BODY_BYTES",
    "EXIT_DISABLED",
]
