"""OPS-114 HMAC authentication for the standalone Plaud webhook receiver.

Mirrors the generic V2 scheme already used by gateway/platforms/webhook.py
(``X-Webhook-Signature-V2`` + ``X-Webhook-Timestamp``, HMAC-SHA256 over
``"<timestamp>.<body>"``, 300s replay tolerance) so this narrow standalone
receiver stays consistent with the rest of the fleet's webhook auth even
though it does not run inside the Gateway process. There is no insecure/
bypass mode: every caller, including loopback tests, presents a real HMAC
signature computed with the same secret the receiver holds.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time
from pathlib import Path

_WEBHOOK_SECRET_FILE_NAME = "plaud_webhook_hmac_secret"
_WEBHOOK_SECRET_LENGTH = 32
REPLAY_WINDOW_SECONDS = 300

SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"


def load_or_create_webhook_secret(data_dir: Path) -> bytes:
    """Load (or create) the dedicated HMAC secret for the Plaud webhook
    receiver. 0600 permissions, never emitted, independent key material
    from every other secret this plugin holds (registry.py's activation
    HMAC key, ingestion_state_db.py's identity key)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / _WEBHOOK_SECRET_FILE_NAME
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) != _WEBHOOK_SECRET_LENGTH:
            raise ValueError(
                f"Plaud webhook secret file {key_path} has unexpected length "
                f"{len(key)} (expected {_WEBHOOK_SECRET_LENGTH})."
            )
        return key
    key = secrets.token_bytes(_WEBHOOK_SECRET_LENGTH)
    key_path.write_bytes(key)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    return key


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """Compute the exact signature a valid sender must present. Exposed
    for tests (and for documenting the activation-side Zapier/relay
    contract) -- never used to skip verification."""
    signed_content = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret, signed_content, hashlib.sha256).hexdigest()


def verify_signature(
    secret: bytes,
    *,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    now: float | None = None,
) -> bool:
    """True iff *signature* is a valid, in-window HMAC-SHA256 of
    ``"<timestamp>.<body>"`` under *secret*. Rejects (never falls back to
    an unauthenticated path) on any missing header, malformed timestamp, or
    out-of-window timestamp."""
    if not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    moment = now if now is not None else time.time()
    if abs(moment - ts) > REPLAY_WINDOW_SECONDS:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(signature, expected)


__all__ = [
    "load_or_create_webhook_secret",
    "sign",
    "verify_signature",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "REPLAY_WINDOW_SECONDS",
]
