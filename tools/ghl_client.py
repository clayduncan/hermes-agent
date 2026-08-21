"""Audited GoHighLevel contact-write client — the replacement for MCP writes.

Why this module exists instead of a hook around the MCP tools
=============================================================

GoHighLevel writes used to happen only through MCP tool calls
(``mcp__ghl_chillcabins__contacts_upsert_contact`` and the equivalents for the
``clay_personal``, ``team_duncan`` and ``tracey`` sub-accounts).  An MCP tool call
is dispatched by the harness to a separate server process — it never passes
through this repo's process boundary, so there is no function to wrap and no
pre/post hook this repo can install on it.  A "log around the MCP call" design
would be unenforceable: any caller could still invoke the tool directly and
bypass the log entirely, which is precisely the failure mode the audit log exists
to rule out.

So this module performs the same writes over direct REST against
``services.leadconnectorhq.com``, using the same Private Integration Token the
MCP server uses, with the audit gate built into the write path itself.

**Required behaviour change:** GHL *writes* (contact create/update/delete/upsert,
tag add/remove) go through this client.  Do not call the GHL MCP write tools
directly any more.  The MCP tools remain the right choice for *reads* and for
endpoints this client does not cover — reads mutate nothing and need no audit line.

Ordering is identical to the Graph clients: validate trigger → fetch ``before``
→ append the audit line and fsync → only then call the destination.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hermes_constants import get_hermes_home
from tools.sync_json_http import (
    HttpRequestError,
    RequestFn,
    build_url,
    request_json,
    urllib_request,
)
from tools.write_audit_log import (
    GHL_CONTACTS,
    AuthorizedWrite,
    WriteAuditRecorder,
    require_trigger,
)

GHL_API_BASE_URL = "https://services.leadconnectorhq.com"

#: Every GHL v2 request needs this header alongside the bearer token.
GHL_API_VERSION = "2021-07-28"

#: The sub-accounts wired up today.  The token for each lives in ``~/.hermes/.env``
#: as ``MCP_GHL_<KEY>_API_KEY`` — the same PIT the MCP server uses.
GHL_ACCOUNT_KEYS: tuple[str, ...] = (
    "chillcabins",
    "clay_personal",
    "team_duncan",
    "tracey",
)


def api_key_env_var(account_key: str) -> str:
    return f"MCP_GHL_{account_key.upper()}_API_KEY"


def api_key_from_hermes_env(account_key: str, *, hermes_home: Path | None = None) -> str:
    """Read the sub-account's PIT from ``~/.hermes/.env``.

    Reuses the delegated-auth module's ``.env`` parser (imported lazily so this
    module is importable without ``httpx``) rather than adding a second one.  The
    inherited shell environment is deliberately not consulted, matching how every
    other Hermes credential lookup behaves.
    """
    from tools.microsoft_graph_delegated_auth import _parse_dotenv

    home = hermes_home or get_hermes_home()
    variable = api_key_env_var(account_key)
    value = _parse_dotenv(home / ".env").get(variable, "").strip()
    if not value:
        raise ValueError(
            f"No GoHighLevel API key for account {account_key!r}: set {variable} in "
            f"{home / '.env'}. No audit line was written and no destination API was called."
        )
    return value


def _unwrap_contact(payload: Any) -> Any:
    """GHL wraps single contacts as ``{"contact": {...}}`` on most endpoints."""
    if isinstance(payload, dict) and "contact" in payload:
        return payload["contact"]
    return payload


class GoHighLevelWriteClient:
    """Audited contact writes against one GoHighLevel sub-account."""

    DESTINATION = GHL_CONTACTS
    ACTOR = "ghl_client"

    def __init__(
        self,
        account_key: str,
        *,
        api_key: str | None = None,
        location_id: str | None = None,
        base_url: str = GHL_API_BASE_URL,
        request_fn: RequestFn = urllib_request,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        hermes_home: Path | None = None,
        log_dir: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not account_key or not str(account_key).strip():
            raise ValueError("GoHighLevelWriteClient requires an account_key.")
        self.account_key = str(account_key).strip()
        self.location_id = location_id
        self.base_url = base_url.rstrip("/")
        self.request_fn = request_fn
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self._api_key = api_key
        self._hermes_home = hermes_home
        self.recorder = WriteAuditRecorder(
            destination=self.DESTINATION,
            actor=self.ACTOR,
            log_dir=log_dir,
            clock=clock,
        )

    # ── HTTP ─────────────────────────────────────────────────────────────────

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = api_key_from_hermes_env(
                self.account_key, hermes_home=self._hermes_home
            )
        return self._api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
            # GHL's edge rejects requests with no User-Agent (Cloudflare error 1010).
            "User-Agent": "Hermes-Agent/audited-ghl-writer",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return request_json(
            self.request_fn,
            method,
            build_url(self.base_url, path, params),
            headers=self._headers(),
            json_body=json_body,
            timeout=self.timeout,
            max_retries=self.max_retries,
            sleep=self.sleep,
        )

    def _fetch_after(self, contact_id: str) -> tuple[Any, bool]:
        """Best-effort post-write read-back: ``(state, after_fetch_failed)``.

        Only I/O-shaped failures are absorbed; a bug in this code still raises
        rather than quietly becoming "verify manually".
        """
        try:
            return _unwrap_contact(self._call("GET", f"/contacts/{contact_id}")), False
        except (HttpRequestError, OSError):
            return None, True

    # ── Reads (no audit line — nothing is mutated) ───────────────────────────

    def get_contact(self, contact_id: str) -> Any:
        return _unwrap_contact(self._call("GET", f"/contacts/{contact_id}"))

    def find_duplicate(self, *, email: str | None = None, phone: str | None = None) -> Any:
        """Look up an existing contact by email/phone via ``/contacts/search/duplicate``."""
        if not self.location_id:
            raise ValueError(
                "find_duplicate() needs a location_id: construct "
                "GoHighLevelWriteClient(..., location_id='<sub-account location id>')."
            )
        if not email and not phone:
            raise ValueError("find_duplicate() needs at least one of email or phone.")
        try:
            found = self._call(
                "GET",
                "/contacts/search/duplicate",
                params={"locationId": self.location_id, "email": email, "number": phone},
            )
        except HttpRequestError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _unwrap_contact(found) or None

    # ── Audit gate ───────────────────────────────────────────────────────────

    def _authorize(
        self, *, operation: str, record_id: Any, before: Any, trigger: str
    ) -> AuthorizedWrite:
        """Append the intent line; the destination call is legal only after this returns."""
        return self.recorder.authorize_write(
            operation=operation,
            record_id=record_id,
            before=before,
            trigger=trigger,
        )

    def _with_location(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        if self.location_id and not body.get("locationId"):
            body["locationId"] = self.location_id
        return body

    # ── Writes ───────────────────────────────────────────────────────────────

    def create_contact(self, contact: Mapping[str, Any], *, trigger: str) -> Any:
        """``POST /contacts/`` — replaces the MCP ``contacts_create_contact`` write."""
        require_trigger(trigger)
        body = self._with_location(contact)
        authorized = self._authorize(
            operation="create", record_id=None, before=None, trigger=trigger
        )
        created = _unwrap_contact(self._call("POST", "/contacts/", json_body=body))

        record_id = created.get("id") if isinstance(created, dict) else None
        if record_id:
            after, after_fetch_failed = self._fetch_after(record_id)
        else:
            # No ID came back, so there is nothing to read back and the post-write
            # state cannot be established — which is exactly what the flag marks.
            after, after_fetch_failed = None, True
        authorized.record_outcome(
            record_id=record_id, after=after, after_fetch_failed=after_fetch_failed
        )
        return created

    def update_contact(
        self, contact_id: str, changes: Mapping[str, Any], *, trigger: str
    ) -> Any:
        """``PUT /contacts/{id}`` — replaces the MCP ``contacts_update_contact`` write.

        GHL's contact PUT has replace semantics for array fields (notably ``tags``):
        whatever you omit can be cleared.  ``before`` here is a real pre-write fetch,
        so the audit log preserves what the record looked like either way.
        """
        require_trigger(trigger)
        before = self.get_contact(contact_id)
        authorized = self._authorize(
            operation="update", record_id=contact_id, before=before, trigger=trigger
        )
        updated = _unwrap_contact(
            self._call("PUT", f"/contacts/{contact_id}", json_body=dict(changes))
        )

        after, after_fetch_failed = self._fetch_after(contact_id)
        authorized.record_outcome(after=after, after_fetch_failed=after_fetch_failed)
        return after if not after_fetch_failed else updated

    def delete_contact(self, contact_id: str, *, trigger: str) -> Any:
        """``DELETE /contacts/{id}``.  Returns the pre-delete state."""
        require_trigger(trigger)
        before = self.get_contact(contact_id)
        authorized = self._authorize(
            operation="delete", record_id=contact_id, before=before, trigger=trigger
        )
        self._call("DELETE", f"/contacts/{contact_id}")

        authorized.record_outcome(after=None)
        return before

    def upsert_contact(self, contact: Mapping[str, Any], *, trigger: str) -> Any:
        """``POST /contacts/upsert`` — replaces ``mcp__ghl_*__contacts_upsert_contact``.

        The pre-write lookup is what makes the log honest: it decides whether this
        is a ``create`` or an ``update`` and supplies ``before`` for the update case,
        rather than letting an upsert log every write as a create.
        """
        require_trigger(trigger)
        body = self._with_location(contact)
        existing = self._resolve_existing(body)
        operation = "update" if existing else "create"
        record_id = existing.get("id") if isinstance(existing, dict) else None

        authorized = self._authorize(
            operation=operation, record_id=record_id, before=existing, trigger=trigger
        )
        result = self._call("POST", "/contacts/upsert", json_body=body)

        upserted = _unwrap_contact(result)
        resolved_id = record_id or (upserted.get("id") if isinstance(upserted, dict) else None)
        if resolved_id:
            after, after_fetch_failed = self._fetch_after(resolved_id)
        else:
            # No ID came back, so there is nothing to read back and the post-write
            # state cannot be established — which is exactly what the flag marks.
            after, after_fetch_failed = None, True
        authorized.record_outcome(
            record_id=resolved_id, after=after, after_fetch_failed=after_fetch_failed
        )
        return result

    def add_tags(self, contact_id: str, tags: Sequence[str], *, trigger: str) -> Any:
        """``POST /contacts/{id}/tags`` — logged as an ``update`` to the contact."""
        return self._tag_write("POST", contact_id, tags, trigger=trigger)

    def remove_tags(self, contact_id: str, tags: Sequence[str], *, trigger: str) -> Any:
        """``DELETE /contacts/{id}/tags`` — logged as an ``update`` to the contact."""
        return self._tag_write("DELETE", contact_id, tags, trigger=trigger)

    def _tag_write(
        self, method: str, contact_id: str, tags: Sequence[str], *, trigger: str
    ) -> Any:
        require_trigger(trigger)
        tag_list = [str(tag) for tag in tags]
        if not tag_list:
            raise ValueError(
                "add_tags()/remove_tags() need at least one tag. No audit line was "
                "written and no destination API was called."
            )
        before = self.get_contact(contact_id)
        authorized = self._authorize(
            operation="update", record_id=contact_id, before=before, trigger=trigger
        )
        result = self._call(
            method, f"/contacts/{contact_id}/tags", json_body={"tags": tag_list}
        )

        after, after_fetch_failed = self._fetch_after(contact_id)
        authorized.record_outcome(after=after, after_fetch_failed=after_fetch_failed)
        return result

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_existing(self, body: Mapping[str, Any]) -> Any:
        """Return the live contact this upsert will land on, or ``None`` for a create."""
        contact_id = body.get("id")
        if contact_id:
            try:
                return self.get_contact(str(contact_id))
            except HttpRequestError as exc:
                if exc.status_code == 404:
                    return None
                raise

        email = body.get("email")
        phone = body.get("phone")
        if not email and not phone:
            raise ValueError(
                "upsert_contact() needs an id, email, or phone so the pre-write state "
                "can be fetched — without it the audit log cannot tell a create from an "
                "update. No audit line was written and no destination API was called."
            )
        return self.find_duplicate(email=email, phone=phone)


__all__ = [
    "GHL_ACCOUNT_KEYS",
    "GHL_API_BASE_URL",
    "GHL_API_VERSION",
    "GoHighLevelWriteClient",
    "HttpRequestError",
    "api_key_env_var",
    "api_key_from_hermes_env",
]
