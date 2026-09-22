"""OPS-110 bounded GHL contact context for Claude Code summarization.

Projects a full GHL contact dict down to exactly the fields Clay approved for
handoff to the summarizer: contact ID, first/last name, GHL `type`, tags,
company name when present, and email domain only (never the full email).
Custom fields are included only for labels on the fixed, explicitly approved
allowlist below -- currently empty, so no custom field reaches the
summarizer until a specific field is named and approved in a future change.

This is metadata, not identity inference: the contact was already resolved
by the activation registry before this module is ever called. Nothing here
performs a lookup, search, or match of its own.
"""

from __future__ import annotations

from typing import Any

#: Custom field labels approved for summarizer handoff. Empty by design --
#: widening this requires a new, separately authorized plan naming the exact
#: field label(s).
APPROVED_CUSTOM_FIELD_LABELS: frozenset[str] = frozenset()

#: The fixed, exact set of keys `build_bounded_contact_context` can ever
#: produce (custom_fields is always present, even if empty).
BOUNDED_CONTEXT_FIELDS: frozenset[str] = frozenset(
    {"contact_id", "first_name", "last_name", "type", "tags", "company_name",
     "email_domain", "custom_fields"}
)


def _email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    _, _, domain = email.strip().partition("@")
    domain = domain.strip()
    return domain or None


def _approved_custom_fields(contact: dict[str, Any]) -> dict[str, Any]:
    """Only fields whose label is on APPROVED_CUSTOM_FIELD_LABELS -- and
    only when that label is unambiguously present -- ever reach the output.
    GHL contact custom fields are typically a list of {id/name/key, value}
    entries; this reads defensively and skips anything not shaped that way."""
    raw = contact.get("customFields")
    if not isinstance(raw, list):
        return {}
    out: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = entry.get("name") or entry.get("key")
        if not isinstance(label, str) or label not in APPROVED_CUSTOM_FIELD_LABELS:
            continue
        out[label] = entry.get("value")
    return out


def build_bounded_contact_context(contact: dict[str, Any]) -> dict[str, Any]:
    """Return the exact bounded projection described in the module docstring.
    Always produces exactly BOUNDED_CONTEXT_FIELDS as keys -- optional values
    are None (or an empty dict for custom_fields) rather than omitted, so a
    consumer never has to guess whether a field was dropped or genuinely
    absent."""
    return {
        "contact_id": contact.get("id"),
        "first_name": contact.get("firstName") or None,
        "last_name": contact.get("lastName") or None,
        "type": contact.get("type") or None,
        "tags": [t for t in (contact.get("tags") or []) if isinstance(t, str)],
        "company_name": contact.get("companyName") or None,
        "email_domain": _email_domain(contact.get("email")),
        "custom_fields": _approved_custom_fields(contact),
    }


__all__ = [
    "APPROVED_CUSTOM_FIELD_LABELS",
    "BOUNDED_CONTEXT_FIELDS",
    "build_bounded_contact_context",
]
