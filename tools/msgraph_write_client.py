"""Audited Microsoft Graph write clients for Contacts and To-Do Tasks.

No wrapper existed for Graph contact/task *writes* before this module — the
existing :mod:`tools.microsoft_graph_client` is a generic async REST helper with
no audit gate.  These clients are the only supported way to mutate Graph contacts
and tasks from this repo, because they are the only path that guarantees the
audit line lands first.

Endpoints wrapped:

* ``POST/PATCH/DELETE https://graph.microsoft.com/v1.0/me/contacts[/{id}]``
* ``POST/PATCH/DELETE https://graph.microsoft.com/v1.0/me/todo/lists/{listId}/tasks[/{id}]``

Both require a delegated token with ``Contacts.ReadWrite`` / ``Tasks.ReadWrite``
(see ``scripts/microsoft_graph_delegated_setup.py``).

Ordering, on every method without exception::

    require_trigger(...)                 # reject a write with no stated reason
    before = GET ...                     # a read; no mutation yet
    authorized = recorder.authorize_write(...)   # audit line fsync'd, or raise
    <destination POST/PATCH/DELETE>      # unreachable unless the line landed
    after = GET ...                      # best-effort read-back
    authorized.record_outcome(...)

The retry loop lives *below* the gate, inside
:func:`tools.sync_json_http.request_json`, so a failed audit append blocks the
first attempt, not merely the last.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.sync_json_http import (
    HttpRequestError,
    RequestFn,
    build_url,
    request_json,
    urllib_request,
)
from tools.write_audit_log import (
    MSGRAPH_CONTACTS,
    MSGRAPH_TASKS,
    AuthorizedWrite,
    WriteAuditRecorder,
    require_trigger,
)

DEFAULT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

TokenProvider = Callable[[], str]


def delegated_token_provider(account_key: str, **provider_kwargs: Any) -> TokenProvider:
    """Adapt the async :class:`DelegatedTokenProvider` to the sync callable used here.

    Import is deferred so this module stays importable (and testable) without
    ``httpx`` installed.  Raises if called from inside a running event loop —
    in that case await the async provider yourself and pass ``lambda: token``.
    """
    from tools.microsoft_graph_delegated_auth import DelegatedTokenProvider

    provider = DelegatedTokenProvider(account_key=account_key, **provider_kwargs)

    def _get_token() -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(provider.get_access_token())
        raise RuntimeError(
            "delegated_token_provider() cannot refresh a token from inside a running "
            "event loop. Await DelegatedTokenProvider.get_access_token() yourself and "
            "pass token_provider=lambda: <token> to the write client instead."
        )

    return _get_token


class _AuditedGraphWriteClient:
    """Shared plumbing for the audited Graph write clients."""

    DESTINATION: str = ""
    ACTOR: str = ""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = DEFAULT_GRAPH_BASE_URL,
        request_fn: RequestFn = urllib_request,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        log_dir: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.request_fn = request_fn
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.recorder = WriteAuditRecorder(
            destination=self.DESTINATION,
            actor=self.ACTOR,
            log_dir=log_dir,
            clock=clock,
        )

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token_provider()}",
            "Accept": "application/json",
            "User-Agent": "Hermes-Agent/audited-graph-writer",
        }

    def _call(self, method: str, path: str, *, json_body: Any = None) -> Any:
        return request_json(
            self.request_fn,
            method,
            build_url(self.base_url, path),
            headers=self._headers(),
            json_body=json_body,
            timeout=self.timeout,
            max_retries=self.max_retries,
            sleep=self.sleep,
        )

    def _fetch_after(self, path: str) -> tuple[Any, bool]:
        """Best-effort post-write read-back: ``(state, after_fetch_failed)``.

        A transport or API failure here is never fatal — the destination write
        already succeeded and must not be reported as failed just because the
        read-back did not land.  Only I/O-shaped failures are absorbed; a bug in
        this code still raises rather than quietly becoming "verify manually".
        """
        try:
            return self._call("GET", path), False
        except (HttpRequestError, OSError):
            return None, True

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


class MicrosoftGraphContactsWriteClient(_AuditedGraphWriteClient):
    """Audited create/update/delete against ``/me/contacts``."""

    DESTINATION = MSGRAPH_CONTACTS
    ACTOR = "msgraph_contacts_client"

    def _contact_path(self, contact_id: str) -> str:
        return f"/me/contacts/{contact_id}"

    def get_contact(self, contact_id: str) -> Any:
        """Read one contact.  A read, not a write — no audit line."""
        return self._call("GET", self._contact_path(contact_id))

    def create_contact(self, contact: Mapping[str, Any], *, trigger: str) -> Any:
        """``POST /me/contacts``.  Returns the created contact as Graph returned it."""
        require_trigger(trigger)
        authorized = self._authorize(
            operation="create", record_id=None, before=None, trigger=trigger
        )
        created = self._call("POST", "/me/contacts", json_body=dict(contact))

        record_id = created.get("id") if isinstance(created, dict) else None
        if record_id:
            after, after_fetch_failed = self._fetch_after(self._contact_path(record_id))
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
        """``PATCH /me/contacts/{id}``.  ``before`` is a real pre-write fetch."""
        require_trigger(trigger)
        path = self._contact_path(contact_id)
        before = self._call("GET", path)
        authorized = self._authorize(
            operation="update", record_id=contact_id, before=before, trigger=trigger
        )
        updated = self._call("PATCH", path, json_body=dict(changes))

        after, after_fetch_failed = self._fetch_after(path)
        authorized.record_outcome(after=after, after_fetch_failed=after_fetch_failed)
        return after if not after_fetch_failed else updated

    def delete_contact(self, contact_id: str, *, trigger: str) -> Any:
        """``DELETE /me/contacts/{id}``.  Returns the pre-delete state."""
        require_trigger(trigger)
        path = self._contact_path(contact_id)
        before = self._call("GET", path)
        authorized = self._authorize(
            operation="delete", record_id=contact_id, before=before, trigger=trigger
        )
        self._call("DELETE", path)

        authorized.record_outcome(after=None)
        return before


class MicrosoftGraphTasksWriteClient(_AuditedGraphWriteClient):
    """Audited create/update/delete against ``/me/todo/lists/{listId}/tasks``."""

    DESTINATION = MSGRAPH_TASKS
    ACTOR = "msgraph_tasks_client"

    def __init__(self, token_provider: TokenProvider, *, list_id: str, **kwargs: Any) -> None:
        if not list_id or not str(list_id).strip():
            raise ValueError("MicrosoftGraphTasksWriteClient requires a To-Do list_id.")
        self.list_id = str(list_id).strip()
        super().__init__(token_provider, **kwargs)

    def _tasks_path(self) -> str:
        return f"/me/todo/lists/{self.list_id}/tasks"

    def _task_path(self, task_id: str) -> str:
        return f"{self._tasks_path()}/{task_id}"

    def get_task(self, task_id: str) -> Any:
        """Read one task.  A read, not a write — no audit line."""
        return self._call("GET", self._task_path(task_id))

    def create_task(self, task: Mapping[str, Any], *, trigger: str) -> Any:
        """``POST /me/todo/lists/{listId}/tasks``."""
        require_trigger(trigger)
        authorized = self._authorize(
            operation="create", record_id=None, before=None, trigger=trigger
        )
        created = self._call("POST", self._tasks_path(), json_body=dict(task))

        record_id = created.get("id") if isinstance(created, dict) else None
        if record_id:
            after, after_fetch_failed = self._fetch_after(self._task_path(record_id))
        else:
            # No ID came back, so there is nothing to read back and the post-write
            # state cannot be established — which is exactly what the flag marks.
            after, after_fetch_failed = None, True
        authorized.record_outcome(
            record_id=record_id, after=after, after_fetch_failed=after_fetch_failed
        )
        return created

    def update_task(self, task_id: str, changes: Mapping[str, Any], *, trigger: str) -> Any:
        """``PATCH /me/todo/lists/{listId}/tasks/{id}``."""
        require_trigger(trigger)
        path = self._task_path(task_id)
        before = self._call("GET", path)
        authorized = self._authorize(
            operation="update", record_id=task_id, before=before, trigger=trigger
        )
        updated = self._call("PATCH", path, json_body=dict(changes))

        after, after_fetch_failed = self._fetch_after(path)
        authorized.record_outcome(after=after, after_fetch_failed=after_fetch_failed)
        return after if not after_fetch_failed else updated

    def delete_task(self, task_id: str, *, trigger: str) -> Any:
        """``DELETE /me/todo/lists/{listId}/tasks/{id}``.  Returns the pre-delete state."""
        require_trigger(trigger)
        path = self._task_path(task_id)
        before = self._call("GET", path)
        authorized = self._authorize(
            operation="delete", record_id=task_id, before=before, trigger=trigger
        )
        self._call("DELETE", path)

        authorized.record_outcome(after=None)
        return before


__all__ = [
    "DEFAULT_GRAPH_BASE_URL",
    "HttpRequestError",
    "MicrosoftGraphContactsWriteClient",
    "MicrosoftGraphTasksWriteClient",
    "delegated_token_provider",
]
