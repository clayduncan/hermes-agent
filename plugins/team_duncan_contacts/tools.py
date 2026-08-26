"""Agent-facing tool implementations for the team_duncan_contacts plugin.

Exposes exactly two tools:
- prepare_activation: resolve identity + issue a confirmation token
- confirm_activation: consume token, timestamp, and record activation

Neither tool accepts raw phone numbers, email addresses, or Apple handles.
All outputs are sanitized before returning.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

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
