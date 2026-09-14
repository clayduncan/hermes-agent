"""GHL read-only contact accessor interface for the team_duncan_contacts plugin.

Defines the protocol (interface) that the registry uses to resolve contacts
during prepare_activation.  The live implementation calls GHL REST read endpoints
(no write capability); tests inject a FakeGhlReader.

Raw handles (phone, email) are only visible inside this module's implementations.
They are never returned by the registry or passed to agent-facing surfaces.
"""

from __future__ import annotations

import re
from typing import Any


class GhlContactReader:
    """Base/protocol class for GHL contact reads.

    Subclass or duck-type this.  Only read methods are defined.
    """

    def get_contact_by_id(self, contact_id: str) -> dict[str, Any] | None:
        """Return the GHL contact dict for *contact_id*, or None if not found."""
        raise NotImplementedError

    def search_contacts_by_name(
        self, query: str, location_id: str
    ) -> list[dict[str, Any]]:
        """Return GHL contact dicts whose name matches *query* within *location_id*."""
        raise NotImplementedError


class ScopedGhlReader(GhlContactReader):
    """Adapts a scoped ``GoHighLevelWriteClient`` to the ``GhlContactReader`` protocol.

    The client is already fixed to one account/location at construction (see
    ``tools.ghl_client.scoped_client``), so a lookup here goes through the same
    reusable scope boundary contact writes do and can never cross it: a
    contact from another sub-account comes back as "not found", never as a
    result.  Network/transport failures are swallowed here (not by the client)
    so a GHL outage degrades to "no match" for the registry rather than
    raising through the agent-facing tool handlers.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_contact_by_id(self, contact_id: str) -> dict[str, Any] | None:
        try:
            return self._client.get_contact_in_scope(contact_id)
        except Exception:
            return None

    def search_contacts_by_name(
        self, query: str, location_id: str
    ) -> list[dict[str, Any]]:
        # location_id is accepted only for protocol compatibility with
        # FakeGhlReader; this client is already pinned to its own location.
        try:
            return self._client.search_contacts(query)
        except Exception:
            return []


class FakeGhlReader(GhlContactReader):
    """In-memory GHL reader for isolated tests.

    Contacts are provided at construction time as plain dicts with at minimum
    the keys: ``id``, ``locationId``, ``firstName``, ``lastName``.  Optional
    ``phone`` and ``email`` keys are used for HMAC-index tests (they remain
    inside the reader boundary; the registry never receives them as return
    values from prepare_activation).
    """

    def __init__(self, contacts: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, dict[str, Any]] = {c["id"]: c for c in contacts}
        self._contacts = contacts

    def get_contact_by_id(self, contact_id: str) -> dict[str, Any] | None:
        return self._by_id.get(contact_id)

    def search_contacts_by_name(
        self, query: str, location_id: str
    ) -> list[dict[str, Any]]:
        q = query.strip().lower()
        results = []
        for c in self._contacts:
            if c.get("locationId") != location_id:
                continue
            first = c.get("firstName", "")
            last = c.get("lastName", "")
            full = f"{first} {last}".strip().lower()
            if q in full or full in q:
                results.append(c)
        return results


def extract_handle_kinds(contact: dict[str, Any]) -> dict[str, str]:
    """Return {kind: raw_handle} for handles present on the GHL contact dict.

    Only called inside the registry's prepare boundary.  The returned dict is
    consumed immediately for HMAC computation and masking; it is never stored
    or returned externally.
    """
    kinds: dict[str, str] = {}
    phone = (contact.get("phone") or "").strip()
    if phone:
        kinds["phone"] = phone
    email = (contact.get("email") or "").strip()
    if email:
        kinds["email"] = email
    return kinds


def contact_display_name(contact: dict[str, Any]) -> str:
    """Return a display name from the GHL contact dict fields."""
    first = (contact.get("firstName") or "").strip()
    last = (contact.get("lastName") or "").strip()
    full = f"{first} {last}".strip()
    if full:
        return full
    name = (contact.get("name") or "").strip()
    if name:
        return name
    return contact.get("id", "Unknown")
