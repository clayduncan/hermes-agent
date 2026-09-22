"""iMessage CRM-lane collector for the team_duncan_contacts plugin (OPS-76).

Invokes the shared, read-only iMessage extractor as a subprocess, then routes
every returned raw event through the existing, unmodified OPS-72/OPS-74 gate
and ledger: registry.resolve_event() followed by ledger.record_event(). This
module adds no second gate, no second ledger, and no dedup/cursor state of
its own: overlap idempotency and pre-activation admission are handled
entirely by record_event()'s own UNIQUE-constraint and approved_overrides
logic. Raw handles are used only as arguments to resolve_event()/
record_event() and are never placed in provenance, logs, or return values.

Expected raw event shape (one dict per row from the shared extractor):
    source_event_id: str       - stable ledger source event id (Apple message.guid)
    occurred_at: str | datetime - event timestamp (ISO-8601 string or datetime)
    is_from_me: bool           - direction; carried through unmodified
    is_group_chat: bool        - True when the thread has more than one participant
    chat_guid: str | None
    handle_raw: str | None     - single counterparty handle (1:1, or the sender
                                  of an inbound group message)
    participant_handles: list[str] - full participant list; only read for
                                      outbound (is_from_me) group-chat events,
                                      where no single counterparty handle exists
    attachment_kinds: list[str]
    has_attachments: bool

OPS-75: the module's ``text`` field is now read (see collect()/
_resolve_and_record()) and passed transiently into note_mirror's
mirror_message_event() call for a genuinely newly admitted event -- never
written to provenance, never logged, never stored. See note_mirror.py for
the OPS-75 iMessage-lane note mirror this feeds.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tools.ghl_client import TEAM_DUNCAN_LOCATION_ID

from ..note_mirror import OK_OUTCOMES as _NOTE_MIRROR_OK_OUTCOMES
from ..note_mirror import contact_detail_url

SOURCE = "imessage"

#: Bound on --window-minutes for the one-shot exact-source-event path (§5):
#: "bounded," not an open sweep.
_ONE_SHOT_MAX_WINDOW_MINUTES = 24 * 60


@dataclass
class CollectSummary:
    """Outcome counts for one collect() invocation."""

    events_seen: int = 0
    admitted: int = 0
    override_admitted: int = 0
    discarded: int = 0
    extractor_invocations: int = 0
    notes_created: int = 0
    notes_recovered: int = 0
    note_errors: int = 0
    note_references: list[dict[str, str]] = field(default_factory=list)


def _coerce_event_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _build_provenance(raw_event: dict) -> dict:
    return {
        "direction": "outbound" if raw_event.get("is_from_me") else "inbound",
        "is_group_chat": bool(raw_event.get("is_group_chat")),
        "chat_guid": raw_event.get("chat_guid"),
        "attachment_kinds": list(raw_event.get("attachment_kinds") or []),
        "has_attachments": bool(raw_event.get("has_attachments")),
    }


def _resolve_and_record(
    registry: Any,
    ledger: Any,
    *,
    raw_handle: str,
    event_ts: datetime,
    source_event_id: str,
    provenance: dict,
    summary: CollectSummary,
    text: str = "",
    is_from_me: bool = False,
    note_mirror: Any | None = None,
) -> None:
    """Resolve then record exactly one (raw_handle, event) pair.

    raw_handle is used only in this function's own scope: once passed to
    resolve_event() and record_event(), it is discarded and never reused,
    mirroring activity_ledger.record_event()'s own discard pattern.

    *text* is a local variable read once from the raw event and passed
    straight through to note_mirror.mirror_message_event() for a genuinely
    newly admitted event only -- never assigned to provenance, never
    logged, never stored.
    """
    summary.events_seen += 1
    result = registry.resolve_event(raw_handle, event_ts)
    ledger_result = ledger.record_event(
        source=SOURCE,
        source_event_id=source_event_id,
        raw_handle=raw_handle,
        event_ts=event_ts,
        provenance=provenance,
    )
    raw_handle = None  # noqa: F841 - explicit discard, no further use

    if ledger_result.outcome == "admitted":
        summary.admitted += 1
    elif ledger_result.outcome == "override_admitted":
        summary.override_admitted += 1
    else:
        summary.discarded += 1
        return

    if not ledger_result.genuine_insert or note_mirror is None:
        return

    mirror_outcome = note_mirror.mirror_message_event(
        event_id=ledger_result.event_id,
        contact_id=result.ghl_contact_id,
        text=text,
        occurred_at=event_ts,
        is_from_me=is_from_me,
        trigger=f"OPS-75 imessage note mirror for ledger event {ledger_result.event_id}",
    )
    if mirror_outcome.outcome not in _NOTE_MIRROR_OK_OUTCOMES:
        summary.note_errors += 1
        return
    if mirror_outcome.outcome == "created":
        summary.notes_created += 1
    elif mirror_outcome.outcome == "recovered":
        summary.notes_recovered += 1
    if mirror_outcome.outcome in ("created", "recovered"):
        summary.note_references.append(
            {
                "note_id": mirror_outcome.note_id,
                "contact_id": result.ghl_contact_id,
                "contact_url": contact_detail_url(TEAM_DUNCAN_LOCATION_ID, result.ghl_contact_id),
            }
        )


def collect(
    registry: Any,
    ledger: Any,
    *,
    window_start: datetime,
    window_end: datetime,
    extract_runner: Callable[[datetime, datetime], list[dict]] | None = None,
    note_mirror: Any | None = None,
) -> CollectSummary:
    """Extract one window of iMessage events and route each into the ledger.

    Calls the shared extractor exactly once (via extract_runner, or the
    default subprocess-based runner), then for every returned raw event calls
    registry.resolve_event() followed by ledger.record_event(). No new
    decision branch is added: record_event() alone decides admit/discard.

    For an outbound (is_from_me) group-chat event, no single counterparty
    handle exists on the row, so this function iterates the event's full
    participant_handles list and resolves/records each participant
    independently, each as its own ledger row (a non-activated participant
    never causes an event to be admitted for another participant).

    *note_mirror* (OPS-75) defaults to None -- every pre-existing caller/test
    is unaffected. When given, a genuinely newly admitted/override-admitted
    event (ledger_result.genuine_insert) is mirrored to a GHL contact note;
    a replayed event (genuine_insert=False, e.g. from an overlapping window)
    is never re-mirrored.
    """
    runner = extract_runner or _default_extract_runner
    raw_events = runner(window_start, window_end)

    summary = CollectSummary(extractor_invocations=1)

    for raw_event in raw_events:
        source_event_id = raw_event["source_event_id"]
        event_ts = _coerce_event_ts(raw_event["occurred_at"])
        provenance = _build_provenance(raw_event)
        is_group_chat = bool(raw_event.get("is_group_chat"))
        is_from_me = bool(raw_event.get("is_from_me"))
        text = raw_event.get("text") or ""

        if is_group_chat and is_from_me:
            participant_handles = list(raw_event.get("participant_handles") or [])
            for index, participant_handle in enumerate(participant_handles):
                _resolve_and_record(
                    registry,
                    ledger,
                    raw_handle=participant_handle,
                    event_ts=event_ts,
                    source_event_id=f"{source_event_id}:{index}",
                    provenance=provenance,
                    summary=summary,
                    text=text,
                    is_from_me=is_from_me,
                    note_mirror=note_mirror,
                )
            continue

        raw_handle = raw_event.get("handle_raw")
        if not raw_handle:
            summary.discarded += 1
            continue

        _resolve_and_record(
            registry,
            ledger,
            raw_handle=raw_handle,
            event_ts=event_ts,
            source_event_id=source_event_id,
            provenance=provenance,
            summary=summary,
            text=text,
            is_from_me=is_from_me,
            note_mirror=note_mirror,
        )

    return summary


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def _default_extract_runner(window_start: datetime, window_end: datetime) -> list[dict]:
    """Invoke the shared read-only extractor as a subprocess.

    Matches the existing sys.executable subprocess pattern used elsewhere in
    this codebase to invoke a stdlib-only wrapper script. The extractor's own
    file is scripts/imessage_desk_extract/extract.py under HERMES_HOME,
    sibling to this plugin's own package root.
    """
    extractor_path = _hermes_home() / "scripts" / "imessage_desk_extract" / "extract.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(extractor_path),
            "--window-start-iso",
            window_start.isoformat(),
            "--window-end-iso",
            window_end.isoformat(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _build_note_mirror(hermes_home: Path) -> Any:
    """Construct the real, audited OPS-75 note mirror: the same
    IngestionStateDb note_mirror table the Desk lane already shares (see
    ingestion_state_db.py -- no migration was needed, it is already
    source-agnostic) plus the audited scoped GHL client, exactly the
    precedent __init__.py::_build_note_mirror_ghl_client already establishes
    for the Desk lane.
    """
    from tools.ghl_client import TEAM_DUNCAN_ACCOUNT_KEY, scoped_client

    from ..ingestion_state_db import IngestionStateDb
    from ..note_mirror import NoteMirror

    data_dir = hermes_home / "plugin-data" / "team_duncan_contacts"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_db = IngestionStateDb(data_dir / "ingestion_state.db")
    ghl_client = scoped_client(
        TEAM_DUNCAN_ACCOUNT_KEY, TEAM_DUNCAN_LOCATION_ID, hermes_home=hermes_home
    )
    return NoteMirror(ghl_client, state_db)


def _run_one_shot(registry: Any, ledger: Any, note_mirror: Any, args: argparse.Namespace) -> int:
    """Bounded, exact-source-event one-shot path (§5): a single named event,
    run through the exact same collect() path as production (same registry
    resolution / ledger admission / genuine_insert-gated mirror call), never
    an open sweep. Creates at most one note. No contact, phone number, name,
    or other person-identifying literal is hard-coded here -- which contact
    it resolves to is purely a runtime result of registry.resolve_event()
    against already-activated registry state.
    """
    if not args.around_iso:
        print(json.dumps({"error": "--around-iso is required together with --source-event-id."}))
        return 1

    window_minutes = min(max(int(args.window_minutes), 1), _ONE_SHOT_MAX_WINDOW_MINUTES)
    around = datetime.fromisoformat(args.around_iso)
    if around.tzinfo is None:
        around = around.replace(tzinfo=timezone.utc)
    half_window = timedelta(minutes=window_minutes / 2)
    window_start = around - half_window
    window_end = around + half_window

    raw_events = _default_extract_runner(window_start, window_end)
    matches = [e for e in raw_events if e.get("source_event_id") == args.source_event_id]

    if len(matches) == 0:
        print(json.dumps({"error": "no matching source event found in window", "source_event_id": args.source_event_id}))
        return 1
    if len(matches) > 1:
        print(
            json.dumps(
                {
                    "error": "multiple matching source events found; refusing to guess",
                    "source_event_id": args.source_event_id,
                    "match_count": len(matches),
                }
            )
        )
        return 1

    matched_event = matches[0]

    def _one_shot_extract_runner(_window_start: datetime, _window_end: datetime) -> list[dict]:
        return [matched_event]

    summary = collect(
        registry, ledger, window_start=window_start, window_end=window_end,
        extract_runner=_one_shot_extract_runner, note_mirror=note_mirror,
    )

    if summary.note_references:
        ref = summary.note_references[0]
        print(json.dumps({"note_id": ref["note_id"], "contact_url": ref["contact_url"]}))
        return 0

    print(
        json.dumps(
            {
                "error": "event did not result in a mirrored note this run",
                "admitted": summary.admitted,
                "override_admitted": summary.override_admitted,
                "discarded": summary.discarded,
                "note_errors": summary.note_errors,
            }
        )
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python3 -m plugins.team_duncan_contacts.collectors.imessage_collector."""
    from ..activity_ledger import ActivityLedger
    from ..registry import ContactRegistry

    parser = argparse.ArgumentParser(description="Collect iMessage events into the Team Duncan activity ledger.")
    parser.add_argument("--window-hours", type=int, default=168)
    parser.add_argument(
        "--source-event-id", type=str, default=None,
        help="Bounded one-shot mode: mirror exactly one named source event "
             "(by its stable ledger source_event_id) through the same collect() "
             "path used in production. Requires --around-iso.",
    )
    parser.add_argument(
        "--around-iso", type=str, default=None,
        help="One-shot mode only: the message's known approximate timestamp (ISO-8601).",
    )
    parser.add_argument(
        "--window-minutes", type=int, default=60,
        help="One-shot mode only: bound (minutes) around --around-iso to search "
             f"(capped at {_ONE_SHOT_MAX_WINDOW_MINUTES}).",
    )
    args = parser.parse_args(argv)

    hermes_home = _hermes_home()
    registry = ContactRegistry(hermes_home, team_duncan_location_id=TEAM_DUNCAN_LOCATION_ID)
    ledger = ActivityLedger(
        hermes_home / "plugin-data" / "team_duncan_contacts" / "activity.db",
        registry,
    )
    note_mirror = _build_note_mirror(hermes_home)

    if args.source_event_id is not None:
        return _run_one_shot(registry, ledger, note_mirror, args)

    window_end = datetime.now(tz=timezone.utc)
    window_start = window_end - timedelta(hours=args.window_hours)

    summary = collect(
        registry, ledger, window_start=window_start, window_end=window_end,
        note_mirror=note_mirror,
    )
    print(
        json.dumps(
            {
                "events_seen": summary.events_seen,
                "admitted": summary.admitted,
                "override_admitted": summary.override_admitted,
                "discarded": summary.discarded,
                "extractor_invocations": summary.extractor_invocations,
                "notes_created": summary.notes_created,
                "notes_recovered": summary.notes_recovered,
                "note_errors": summary.note_errors,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
