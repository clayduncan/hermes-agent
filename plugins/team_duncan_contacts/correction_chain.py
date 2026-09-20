"""OPS-18 wrong-contact correction chain.

Corrects a completed pending_review whose event was admitted to the wrong
contact. Never deletes or rewrites prior history: revokes the old grant,
then composes with the ledger's own existing `correct_event` primitive
(OPS-74) to supersede the old ledger row and insert a new one for the
corrected contact, under a new correction-tagged source_event_id (the
ledger's event_id is deterministic from (source, source_event_id), so a
genuinely different admission needs a genuinely different key -- reusing
the original key could only ever be silently ignored by the ledger's own
INSERT OR IGNORE, never overwrite it).

A hard boundary this respects: `activity_ledger.py` cannot be modified, and
`record_event`/`correct_event` only ever admit an event to whichever exact
contact a raw handle's HMAC uniquely resolves to via `resolve_event`. There
is no path through the existing, frozen ledger API to force-admit an event
to a contact whose registered handle does not match the raw handle being
resolved -- an override grant only bypasses the *activation cutoff* for an
otherwise-unique match, it does not (and structurally cannot, without
editing activity_ledger.py) redirect resolution to a different contact.
Consequently, this module only supports correction where the corrected
contact's own resolution comes back `allow` for a *fresh* raw handle --
the realistic case is a source re-identification (the original exact-event
lookup or Plaud identity resolved to the wrong underlying event/handle,
and a fresh sealed re-fetch reveals the corrected one). If the fresh
resolution does not come back `allow`, this raises rather than silently
leaving the correction half-applied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .ingestion_state_db import IngestionStateDb, STATUS_COMPLETE, STATUS_REPLAYED


class CorrectionNotEligibleError(RuntimeError):
    pass


class CorrectionFailedError(RuntimeError):
    """The corrected contact's fresh resolution did not come back `allow`;
    no ledger row was superseded or created, and the old grant was not
    revoked (revocation only happens once a replacement is confirmed)."""


def correction_source_event_id(original_source_event_id: str, correction_seq: int) -> str:
    return f"{original_source_event_id}#correction:{correction_seq}"


def correct_wrong_contact(
    *,
    state_db: IngestionStateDb,
    activity_ledger: Any,
    pending_review_id: str,
    new_contact_id: str,
    raw_handle: str,
    approval_reason: str,
    correction_seq: int = 1,
) -> dict[str, Any]:
    """Supersede the old ledger row and admit a new one for *new_contact_id*.

    *raw_handle* must come from a fresh sealed re-fetch (the same mechanism
    ingestion_runner uses for every other privileged action) and must
    resolve, via `resolve_event`, to `allow` for *new_contact_id* specifically
    -- otherwise this raises CorrectionFailedError and changes nothing.
    *approval_reason* is accepted for symmetry with the grant-based flows and
    recorded in this chain's own annotation; the `allow` path itself needs no
    override grant.

    Returns {"outcome": "corrected", "new_source_event_id": str,
    "ledger_outcome": "corrected" | "noop"}.
    """
    row = state_db.get_pending_review(pending_review_id)
    if row is None:
        raise CorrectionNotEligibleError(f"pending_review {pending_review_id!r} not found.")
    if row.status not in (STATUS_COMPLETE, STATUS_REPLAYED):
        raise CorrectionNotEligibleError(
            f"pending_review {pending_review_id!r} has not completed; nothing to correct."
        )
    old_contact_id = row.resolved_contact_id
    if not old_contact_id:
        raise CorrectionNotEligibleError(
            f"pending_review {pending_review_id!r} has no resolved contact to correct away from."
        )
    if old_contact_id == new_contact_id:
        raise CorrectionNotEligibleError("New contact is the same as the current contact.")

    event_ts = datetime.fromisoformat(row.occurred_at)
    new_provenance = {
        "duration_s": row.duration_s, "direction": row.direction, "answered": row.answered,
    }
    new_source_event_id = correction_source_event_id(row.source_event_id, correction_seq)

    correct_result = activity_ledger.correct_event(
        row.source, row.source_event_id, new_source_event_id,
        raw_handle, event_ts, new_provenance,
    )
    if correct_result.outcome not in ("corrected", "noop"):
        raise CorrectionFailedError(
            f"Fresh resolution for pending_review {pending_review_id!r} did not "
            "come back `allow` for the corrected contact; nothing was changed."
        )

    # Only once the replacement is confirmed admitted do we revoke whatever
    # override backed the old admission (a no-op if the old admission was a
    # plain `allow`, never an override).
    activity_ledger.revoke_override(old_contact_id, row.source, row.source_event_id)

    state_db.record_annotation(
        pending_review_id, f"corrected_to:{new_contact_id}:{approval_reason}", actor="clay",
    )

    return {
        "outcome": "corrected",
        "new_source_event_id": new_source_event_id,
        "ledger_outcome": correct_result.outcome,
    }
