"""OPS-110 GHL note writer for Plaud call summaries.

Reuses the exact same local dedupe identity and table the OPS-18 Desk-only
NoteMirror already writes (`note_mirror.py`, `IngestionStateDb.note_mirror`):
`event_id = sha256(f"{SOURCE_DESK_CALL}|{desk_source_event_id}")`. That is
what lets this module find and update in place the exact note the Desk-only
pipeline may have already created for this call (metadata-only body) rather
than creating a duplicate -- the two pipelines share one row per Desk event.

Every GHL write goes through the audited GoHighLevelWriteClient
(tools.ghl_client) -- create_note/update_note/get_note -- never a direct
unlogged REST call. Every write here uses the fixed CALL_NOTE_COLOR (the
same light green every call note in this build uses) and a metadata-only
title; the body passed in (`summary_lines`, despite the name -- see
plaud_summary_runner.build_visible_body_lines) is the caller's
deterministic discussed/clay_commitment/next_step composition, joined with
newlines and written verbatim -- this module itself never reconstructs or
reorders note text from those fields, which is what prevents it from ever
mixing up who said or committed to what.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .ingestion_state_db import SOURCE_DESK_CALL
from .note_mirror import CALL_NOTE_COLOR

log = logging.getLogger(__name__)


def desk_event_id(desk_source_event_id: str) -> str:
    """The exact identity formula activity_ledger.py and note_mirror.py
    already use for a Desk-sourced event. Reusing it (never inventing a
    second identity scheme) is the whole mechanism behind "update the exact
    note, never duplicate it"."""
    return hashlib.sha256(
        f"{SOURCE_DESK_CALL}|{desk_source_event_id}".encode("utf-8")
    ).hexdigest()


@dataclass
class NoteWriteResult:
    outcome: str  # 'created' | 'updated' | 'already_current' | 'error'
    note_id: str | None = None


class PlaudSummaryNoteWriter:
    """Idempotent create-or-update-in-place writer for one Desk-event note.

    *ghl_client* is a scoped GoHighLevelWriteClient (create_note/update_note/
    get_note); *state_db* is the OPS-18 IngestionStateDb's note_mirror table
    (shared with note_mirror.py's NoteMirror).
    """

    def __init__(self, ghl_client: Any, state_db: Any) -> None:
        self._ghl = ghl_client
        self._state_db = state_db

    def write(
        self,
        *,
        desk_source_event_id: str,
        contact_id: str,
        title: str,
        summary_lines: list[str],
        trigger: str,
    ) -> NoteWriteResult:
        event_id = desk_event_id(desk_source_event_id)
        body = "\n".join(summary_lines)
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        existing = self._state_db.get_note_mirror(event_id)

        if existing is not None and existing.status == "complete" and existing.note_id:
            if existing.content_hash == content_hash:
                return self._verify_current(event_id, existing.note_id, contact_id)
            return self._update(
                event_id, existing.note_id, contact_id, title, body, content_hash, trigger
            )

        self._state_db.record_note_mirror_attempt(
            event_id, contact_id=contact_id, content_hash=content_hash
        )
        return self._create(event_id, contact_id, title, body, content_hash, trigger)

    def _create(
        self, event_id: str, contact_id: str, title: str, body: str,
        content_hash: str, trigger: str,
    ) -> NoteWriteResult:
        try:
            created = self._ghl.create_note(
                contact_id, body, trigger=trigger, color=CALL_NOTE_COLOR, title=title
            )
        except Exception as exc:
            log.error("plaud_note_writer: create_note failed [%s]", type(exc).__name__)
            return NoteWriteResult(outcome="error")

        note_id = created.get("id") if isinstance(created, dict) else None
        if not note_id:
            log.error("plaud_note_writer: create_note returned no note id")
            return NoteWriteResult(outcome="error")

        try:
            readback = self._ghl.get_note(contact_id, note_id)
        except Exception as exc:
            log.error("plaud_note_writer: read-back failed [%s]", type(exc).__name__)
            return NoteWriteResult(outcome="error", note_id=note_id)

        readback_body = readback.get("body") if isinstance(readback, dict) else None
        if readback_body != body:
            log.error("plaud_note_writer: read-back body did not match what was written")
            return NoteWriteResult(outcome="error", note_id=note_id)

        self._state_db.mark_note_mirror_complete(
            event_id, contact_id=contact_id, note_id=note_id, content_hash=content_hash,
        )
        return NoteWriteResult(outcome="created", note_id=note_id)

    def _update(
        self, event_id: str, note_id: str, contact_id: str, title: str, body: str,
        content_hash: str, trigger: str,
    ) -> NoteWriteResult:
        try:
            updated = self._ghl.update_note(
                contact_id, note_id, body, trigger=trigger, color=CALL_NOTE_COLOR, title=title
            )
        except Exception as exc:
            log.error("plaud_note_writer: update_note failed [%s]", type(exc).__name__)
            return NoteWriteResult(outcome="error", note_id=note_id)

        updated_body = updated.get("body") if isinstance(updated, dict) else None
        if updated_body != body:
            log.error("plaud_note_writer: update read-back body did not match what was written")
            return NoteWriteResult(outcome="error", note_id=note_id)

        self._state_db.mark_note_mirror_complete(
            event_id, contact_id=contact_id, note_id=note_id, content_hash=content_hash,
        )
        return NoteWriteResult(outcome="updated", note_id=note_id)

    def _verify_current(self, event_id: str, note_id: str, contact_id: str) -> NoteWriteResult:
        """The local row already claims this exact summary is on this note.
        Read it back rather than trusting local state blindly before
        reporting "no write needed"."""
        try:
            readback = self._ghl.get_note(contact_id, note_id)
        except Exception as exc:
            log.error("plaud_note_writer: verify read failed [%s]", type(exc).__name__)
            return NoteWriteResult(outcome="error", note_id=note_id)
        if not isinstance(readback, dict) or not readback.get("id"):
            log.error("plaud_note_writer: existing note no longer resolves")
            return NoteWriteResult(outcome="error", note_id=note_id)
        return NoteWriteResult(outcome="already_current", note_id=note_id)


__all__ = ["NoteWriteResult", "PlaudSummaryNoteWriter", "desk_event_id"]
