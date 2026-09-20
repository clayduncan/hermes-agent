"""OPS-18 notification payloads and delivery.

Telegram is primary, email is fallback-only. Injectable `Notifier`
interfaces so build and tests never perform a real send. Every payload
builder here is a dedicated safe projection: it can only carry the exact
approved fields for its case, never a raw handle, contact ID, transcript,
summary, mind map, provenance blob, or unnecessary borrower information.
"""

from __future__ import annotations

from typing import Any, Protocol

from .ingestion_state_db import PendingReviewRow
from .sanitizer import sanitize_output


class Notifier(Protocol):
    """Injectable delivery boundary. Returns True iff delivery succeeded.
    Production implementations talk to Telegram/email; this build's tests
    only ever inject fakes, and nothing here constructs a live sender."""

    def send(self, payload: dict[str, Any]) -> bool: ...


class TelegramPrimaryEmailFallbackNotifier:
    """Tries the injected Telegram notifier first; falls back to email only
    if Telegram delivery fails. Never sends both for the same attempt."""

    def __init__(self, telegram: Notifier, email: Notifier) -> None:
        self._telegram = telegram
        self._email = email

    def send(self, payload: dict[str, Any]) -> bool:
        if self._telegram.send(payload):
            return True
        return self._email.send(payload)


def _base_fields(row: PendingReviewRow) -> dict[str, Any]:
    return {
        "source": row.source,
        "occurred_at": row.occurred_at,
        "duration_s": row.duration_s,
        "direction": row.direction,
    }


def _sanitize_except(payload: dict[str, Any], preserve: tuple[str, ...]) -> dict[str, Any]:
    """Sanitize every top-level field of *payload* except the exact keys in
    *preserve*, which are returned byte-identical. Scoped per call site to
    the deterministic, system-generated identifier fields named in
    *preserve*; never a content-shape or generic key-name bypass."""
    return {
        k: (v if k in preserve else sanitize_output(v))
        for k, v in payload.items()
    }


def build_deny_pre_activation_payload(row: PendingReviewRow) -> dict[str, Any]:
    """Known pre-activation event: approval prompt for an override grant."""
    payload = {
        **_base_fields(row),
        "display_name": row.display_name,
        "masked_labels": row.masked_labels,
        "prompt": (
            "This call occurred before the contact's activation cutoff. "
            "Approve an override grant to admit it, or dismiss."
        ),
        "pending_review_id": row.id,
    }
    return _sanitize_except(payload, ("pending_review_id",))


def build_plaud_zero_match_payload(row: PendingReviewRow, masked_source_label: str | None) -> dict[str, Any]:
    """Plaud zero-match: no contact identified yet. masked_source_label is
    derived from the still-held raw incoming handle before it is cleared --
    the only context where masking applies to non-contact raw data."""
    payload = {
        **_base_fields(row),
        "masked_source_label": masked_source_label,
        "prompt": "No registry contact matched this Plaud call. Create a contact or dismiss.",
        "pending_review_id": row.id,
    }
    return _sanitize_except(payload, ("pending_review_id",))


def build_multiple_match_payload(
    row: PendingReviewRow, candidate_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Non-collision multiple_match: each candidate's pre-rendered
    display_label verbatim, plus its selection_token. candidate_rows are
    never persisted; they are handed off transiently at notify time only."""
    candidates = [
        {
            "selection_token": c["selection_token"],
            "display_label": sanitize_output(c["display_label"]),
        }
        for c in candidate_rows
    ]
    payload = {
        **_base_fields(row),
        "candidates": candidates,
        "prompt": "Multiple registry contacts matched. Select exactly one candidate.",
        "pending_review_id": row.id,
    }
    # "candidates" is pre-sanitized above (display_label sanitized per entry,
    # selection_token preserved per entry); exempt it here from a second,
    # blanket sanitize_output pass so its already-correct selection_token
    # values are not re-scanned and potentially mangled.
    return _sanitize_except(payload, ("pending_review_id", "candidates"))


def build_collision_payload(row: PendingReviewRow) -> dict[str, Any]:
    """Collision: no candidate data of any kind. Clay must resolve in GHL."""
    payload = {
        **_base_fields(row),
        "prompt": (
            "Multiple registry contacts matched and cannot be safely "
            "distinguished. Resolve directly in GoHighLevel."
        ),
        "pending_review_id": row.id,
    }
    return _sanitize_except(payload, ("pending_review_id",))
