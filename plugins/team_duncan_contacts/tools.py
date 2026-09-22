"""Agent-facing tool implementations for the team_duncan_contacts plugin.

Exposes:
- prepare_activation / confirm_activation: resolve identity + confirm activation
- list_pending_call_reviews: read-only, bounded Pending Call Reviews surface (OPS-18)
- prepare_call_log_ingest / confirm_call_log_ingest / accept_call_log_ingest_run:
  the manual-run gate for call-log ingestion (OPS-18)

None of these tools accept raw phone numbers, email addresses, or Apple
handles as input, or expose one in their output. All outputs are sanitized
before returning. The ingestion trigger tools reject a cron/background
invocation outright and never run on a scheduler.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .ingestion_state_db import SOURCE_DESK_CALL, SOURCE_PLAUD
from .ingestion_state_db import list_pending_call_reviews as _list_pending_call_reviews

log = logging.getLogger(__name__)


def _is_cron_session() -> bool:
    """True when the current session is a cron/background invocation.

    Mirrors the existing tools/approval.py::_is_cron_approval_context
    precedent: prefer the session ContextVar, fall back to the process env
    var for CLI/test contexts that never engage the session-context layer.
    """
    try:
        from gateway.session_context import get_session_env
        from utils import is_truthy_value

        return is_truthy_value(get_session_env("HERMES_CRON_SESSION", ""))
    except Exception:
        from utils import env_var_enabled

        return env_var_enabled("HERMES_CRON_SESSION")

# --- Tool schemas ---

PREPARE_ACTIVATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "prepare_activation",
        "description": (
            "Resolve a Team Duncan contact's identity and prepare a one-time "
            "confirmation token for manual activation approval. "
            "Accepts only a contact's full name or exact GoHighLevel contact ID "
            "(never a phone number, email address, or messaging handle). "
            "Returns masked handle descriptors, the exact GHL contact ID, the "
            "Team Duncan location ID, and a short-lived token. "
            "Activation tracking begins only after confirm_activation is called; "
            "activity before that timestamp is never authorized."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name_or_id": {
                    "type": "string",
                    "description": (
                        "The contact's full name (e.g. 'Jane Smith') or their "
                        "exact GoHighLevel contact ID. "
                        "Do NOT provide a phone number, email address, iMessage "
                        "handle, Apple ID, or any other communication handle: "
                        "those are rejected."
                    ),
                }
            },
            "required": ["name_or_id"],
        },
    },
}

CONFIRM_ACTIVATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "confirm_activation",
        "description": (
            "Confirm a pending contact activation using the opaque token returned "
            "by prepare_activation. Clay must explicitly confirm each activation. "
            "The activation timestamp is generated at confirmation time, never "
            "inherited from the preparation step. Replaying a token for an already-"
            "activated contact is safe and idempotent; the original cutoff is preserved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "The opaque confirmation token returned by prepare_activation. "
                        "Tokens expire after 5 minutes."
                    ),
                }
            },
            "required": ["token"],
        },
    },
}


# --- Handler factories ---

def make_prepare_handler(registry, ghl_reader):
    """Return a handler closure that captures the registry and GHL reader."""

    def prepare_activation(args: dict[str, Any], **_: Any) -> str:
        name_or_id = str(args.get("name_or_id") or "")
        try:
            result = registry.prepare_activation(name_or_id, ghl_reader)
            return json.dumps(result.to_dict(), ensure_ascii=False)
        except Exception as exc:
            log.error("prepare_activation internal error [%s]", type(exc).__name__)
            return json.dumps(
                {
                    "status": "error",
                    "message": "An internal error occurred. No state was written.",
                }
            )

    return prepare_activation


def make_confirm_handler(registry):
    """Return a handler closure that captures the registry."""

    def confirm_activation(args: dict[str, Any], **_: Any) -> str:
        token = str(args.get("token") or "")
        try:
            result = registry.confirm_activation(token)
            return json.dumps(result.to_dict(), ensure_ascii=False)
        except Exception as exc:
            log.error("confirm_activation internal error [%s]", type(exc).__name__)
            return json.dumps(
                {
                    "status": "error",
                    "message": "An internal error occurred. No state was written.",
                }
            )

    return confirm_activation


SET_IMESSAGE_ACTIVATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "set_imessage_activation",
        "description": (
            "Turn the local iMessage Note lane on or off for a Team Duncan "
            "contact, for plain-language commands like 'activate "
            "[name]'s iMessages' and 'deactivate [name]'s iMessages'. "
            "This tool has no GoHighLevel mutation capability: it never "
            "touches GHL Do Not Disturb settings, tags, campaigns, "
            "workflows, or any marketing delivery mechanism, and never adds "
            "a GHL tag. 'deactivate' pauses the contact; it never retires "
            "one, and is safely idempotent if already paused. 'activate' "
            "on a paused contact resumes it, preserving the original "
            "activation cutoff; on a contact never seen before, it reuses "
            "prepare_activation and confirm_activation against the "
            "read-only GHL reader (the calling command is itself the "
            "explicit authorization, so no further confirmation step is "
            "needed here). Accepts only a contact's full name or exact "
            "GoHighLevel contact ID, never a phone number, email address, "
            "Apple ID, or messaging handle. Ambiguous names fail with no "
            "state change; raw handles never appear in the response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["activate", "deactivate"],
                    "description": (
                        "Whether to turn the contact's local iMessage Note "
                        "lane on ('activate') or off ('deactivate')."
                    ),
                },
                "name_or_id": {
                    "type": "string",
                    "description": (
                        "The contact's full name (e.g. 'Jane Smith') or their "
                        "exact GoHighLevel contact ID. "
                        "Do NOT provide a phone number, email address, iMessage "
                        "handle, Apple ID, or any other communication handle: "
                        "those are rejected."
                    ),
                },
            },
            "required": ["action", "name_or_id"],
        },
    },
}


def make_set_imessage_activation_handler(registry, ghl_reader):
    """Return a handler closure that captures the registry and GHL reader.

    No GHL mutation capability: every path here goes through the registry's
    local pause_contact/resume_contact/prepare_activation/confirm_activation
    methods, all of which touch only the injected read-only `ghl_reader`.
    This tool never calls a GHL write client and never touches DND, tags,
    campaigns, workflows, or marketing delivery.
    """

    def set_imessage_activation(args: dict[str, Any], **_: Any) -> str:
        action = str(args.get("action") or "")
        name_or_id = str(args.get("name_or_id") or "")
        try:
            result = registry.set_imessage_activation(action, name_or_id, ghl_reader)
            return json.dumps(result.to_dict(), ensure_ascii=False)
        except Exception as exc:
            log.error("set_imessage_activation internal error [%s]", type(exc).__name__)
            return json.dumps(
                {
                    "status": "error",
                    "message": "An internal error occurred. No state was written.",
                }
            )

    return set_imessage_activation


# ---------------------------------------------------------------------------
# OPS-18: Pending Call Reviews (read-only)
# ---------------------------------------------------------------------------

LIST_PENDING_CALL_REVIEWS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_pending_call_reviews",
        "description": (
            "Read-only Pending Call Reviews queue: bounded, oldest-first rows "
            "of call-log ingestion items awaiting Clay's decision, plus a "
            "total unresolved count. Never shows raw handles, contact IDs, "
            "candidate-token mappings, transcripts, summaries, mind maps, "
            "provenance blobs, or GHL payloads. Has no mutation path and "
            "exposes no action token."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional exact pending_review.status filter.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional exact source filter: 'plaud' or 'desk_call'.",
                },
                "match_outcome": {
                    "type": "string",
                    "description": "Optional exact match_outcome filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum rows to return. Defaults to 50; capped at 200.",
                },
            },
            "required": [],
        },
    },
}


def make_list_pending_call_reviews_handler(state_db):
    """Return a handler closure over the OPS-18 ingestion state DB."""

    def list_pending_call_reviews(args: dict[str, Any], **_: Any) -> str:
        try:
            limit = args.get("limit")
            limit = int(limit) if limit is not None else 50
            result = _list_pending_call_reviews(
                state_db,
                status=args.get("status") or None,
                source=args.get("source") or None,
                match_outcome=args.get("match_outcome") or None,
                limit=limit,
            )
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            log.error("list_pending_call_reviews internal error [%s]", type(exc).__name__)
            return json.dumps(
                {"status": "error", "message": "An internal error occurred."}
            )

    return list_pending_call_reviews


# ---------------------------------------------------------------------------
# OPS-18: manual-run gate for call-log ingestion
# ---------------------------------------------------------------------------

PREPARE_CALL_LOG_INGEST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "prepare_call_log_ingest",
        "description": (
            "Prepare a one-time confirmation token for a manual call-log "
            "ingestion run over this build's enabled sources only (see the "
            "returned `sources` list for the exact set -- currently Desk "
            "CallHistory; Plaud is disabled pending OPS-110 and is never "
            "read). Triggers no source read. Rejected outside an "
            "interactive Clay-confirmed session (never runs from cron or a "
            "background context). Only one run may be outstanding at a time."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CONFIRM_CALL_LOG_INGEST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "confirm_call_log_ingest",
        "description": (
            "Confirm a manual call-log ingestion run using the token from "
            "prepare_call_log_ingest. Consumes the single-use token (5-minute "
            "TTL) and performs the run's first source read. Returns a "
            "sanitized run summary including pending_review_count, "
            "oldest_pending_at, failed_review_count, and oldest_failed_at. "
            "Rejected outside an interactive Clay-confirmed session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "The opaque token returned by prepare_call_log_ingest.",
                }
            },
            "required": ["token"],
        },
    },
}

ACCEPT_CALL_LOG_INGEST_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "accept_call_log_ingest_run",
        "description": (
            "Accept a completed call-log ingestion run, unblocking the next "
            "prepare_call_log_ingest call. Rejected outside an interactive "
            "Clay-confirmed session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id returned by confirm_call_log_ingest.",
                }
            },
            "required": ["run_id"],
        },
    },
}


def make_prepare_call_log_ingest_handler(
    state_db, enabled_sources: tuple[str, ...] = (SOURCE_PLAUD, SOURCE_DESK_CALL)
):
    """*enabled_sources* is fixed internal configuration supplied by the
    plugin factory at registration time, not model input -- it must match
    the same set passed to the IngestionRunner so this tool's reported
    `sources` never claims a source the run itself will not touch. Defaults
    to both sources for backward compatibility with direct construction."""

    def prepare_call_log_ingest(args: dict[str, Any], **_: Any) -> str:
        if _is_cron_session():
            return json.dumps(
                {"status": "rejected", "reason": "cron_context",
                 "message": "Call-log ingestion cannot be triggered from a cron/background context."}
            )
        try:
            unaccepted = state_db.has_unaccepted_run()
            if unaccepted:
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason": "unaccepted_run_outstanding",
                        "run_id": unaccepted,
                        "message": (
                            "A prior ingestion run is awaiting acceptance. Call "
                            "accept_call_log_ingest_run first."
                        ),
                    }
                )
            token, expires_at = state_db.issue_confirmation_token()
            from .collectors.call_history_collector import CALL_HISTORY_LOOKBACK_DAYS

            return json.dumps(
                {
                    "status": "ready_for_confirmation",
                    "token": token,
                    "token_expires_at": expires_at,
                    "sources": list(enabled_sources),
                    "desk_lookback_days": CALL_HISTORY_LOOKBACK_DAYS,
                    "pending_review_backlog": state_db.count_unresolved_pending_review(),
                    "message": "Confirm with confirm_call_log_ingest within 5 minutes to run.",
                }
            )
        except Exception as exc:
            log.error("prepare_call_log_ingest internal error [%s]", type(exc).__name__)
            return json.dumps({"status": "error", "message": "An internal error occurred."})

    return prepare_call_log_ingest


def make_confirm_call_log_ingest_handler(runner_factory):
    """*runner_factory* returns a fresh IngestionRunner (and its state_db)
    each call, so nothing here holds a live transport reference at plugin
    load time."""

    def confirm_call_log_ingest(args: dict[str, Any], **_: Any) -> str:
        if _is_cron_session():
            return json.dumps(
                {"status": "rejected", "reason": "cron_context",
                 "message": "Call-log ingestion cannot be triggered from a cron/background context."}
            )
        token = str(args.get("token") or "")
        try:
            runner, state_db = runner_factory()
            if not state_db.consume_confirmation_token(token):
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason": "token_not_found_or_expired",
                        "message": "Confirmation token not found, expired, or already used.",
                    }
                )
            summary = runner.run(token=token)
            result = summary.to_dict()
            result["status"] = "awaiting_acceptance"
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            log.error("confirm_call_log_ingest internal error [%s]", type(exc).__name__)
            return json.dumps({"status": "error", "message": "An internal error occurred."})

    return confirm_call_log_ingest


def make_accept_call_log_ingest_run_handler(state_db):
    def accept_call_log_ingest_run(args: dict[str, Any], **_: Any) -> str:
        if _is_cron_session():
            return json.dumps(
                {"status": "rejected", "reason": "cron_context",
                 "message": "Call-log ingestion cannot be triggered from a cron/background context."}
            )
        run_id = str(args.get("run_id") or "")
        try:
            accepted = state_db.accept_run(run_id)
            if not accepted:
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason": "run_not_found_or_already_accepted",
                        "message": "No awaiting-acceptance run matches that run_id.",
                    }
                )
            return json.dumps({"status": "accepted", "run_id": run_id})
        except Exception as exc:
            log.error("accept_call_log_ingest_run internal error [%s]", type(exc).__name__)
            return json.dumps({"status": "error", "message": "An internal error occurred."})

    return accept_call_log_ingest_run


# ---------------------------------------------------------------------------
# OPS-110: manual-run gate for Plaud call-summary generation
#
# A deliberately separate, self-contained gate from the OPS-18 call-log
# ingestion one above: its own single-use token (issued from the same
# generic ingestion_confirmations table, but never cross-checked against
# call-log ingestion's own run bookkeeping), and no awaiting-acceptance
# step -- confirm_plaud_summary_run performs the run and returns its
# summary directly, rather than requiring a further accept call.
# ---------------------------------------------------------------------------

PREPARE_PLAUD_SUMMARY_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "prepare_plaud_summary_run",
        "description": (
            "Prepare a one-time confirmation token for a manual Plaud "
            "call-summary run (OPS-110): metadata-only correlation of Plaud "
            "recordings against immutable Desk calls, activation gating, "
            "transcript fetch, Claude Code summarization, and a GHL note "
            "create-or-update. Triggers no source read. Rejected outside "
            "an interactive Clay-confirmed session (never runs from cron "
            "or a background context)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CONFIRM_PLAUD_SUMMARY_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "confirm_plaud_summary_run",
        "description": (
            "Confirm a manual Plaud call-summary run using the token from "
            "prepare_plaud_summary_run. Consumes the single-use token "
            "(5-minute TTL) and performs the full run. Returns a sanitized "
            "run summary (matched/unmatched/ambiguous/skipped/notes_written/"
            "errors counts only). Rejected outside an interactive "
            "Clay-confirmed session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "The opaque token returned by prepare_plaud_summary_run.",
                }
            },
            "required": ["token"],
        },
    },
}


def make_prepare_plaud_summary_run_handler(state_db):
    def prepare_plaud_summary_run(args: dict[str, Any], **_: Any) -> str:
        if _is_cron_session():
            return json.dumps(
                {"status": "rejected", "reason": "cron_context",
                 "message": "Plaud call-summary runs cannot be triggered from a cron/background context."}
            )
        try:
            token, expires_at = state_db.issue_confirmation_token()
            return json.dumps(
                {
                    "status": "ready_for_confirmation",
                    "token": token,
                    "token_expires_at": expires_at,
                    "message": "Confirm with confirm_plaud_summary_run within 5 minutes to run.",
                }
            )
        except Exception as exc:
            log.error("prepare_plaud_summary_run internal error [%s]", type(exc).__name__)
            return json.dumps({"status": "error", "message": "An internal error occurred."})

    return prepare_plaud_summary_run


def make_confirm_plaud_summary_run_handler(runner_factory):
    """*runner_factory* returns a fresh (PlaudSummaryRunner, state_db) pair
    each call, so nothing here holds a live transport or subprocess
    reference at plugin load time."""

    def confirm_plaud_summary_run(args: dict[str, Any], **_: Any) -> str:
        if _is_cron_session():
            return json.dumps(
                {"status": "rejected", "reason": "cron_context",
                 "message": "Plaud call-summary runs cannot be triggered from a cron/background context."}
            )
        token = str(args.get("token") or "")
        try:
            runner, state_db = runner_factory()
            if not state_db.consume_confirmation_token(token):
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason": "token_not_found_or_expired",
                        "message": "Confirmation token not found, expired, or already used.",
                    }
                )
            summary = runner.run(token=token)
            result = summary.to_dict()
            result["status"] = "completed"
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            log.error("confirm_plaud_summary_run internal error [%s]", type(exc).__name__)
            return json.dumps({"status": "error", "message": "An internal error occurred."})

    return confirm_plaud_summary_run
