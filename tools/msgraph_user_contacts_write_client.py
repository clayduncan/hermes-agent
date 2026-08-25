"""Audited app-only Microsoft Graph user contacts write client (OPS-10).

Two classes are provided:

MicrosoftGraphUserContactsWriteClient
    Wraps ``/users/{target_user}/contacts/{contact_id}`` for audited
    ``update_contact`` and ``delete_contact``.  Every write is gated by a
    pre-approved execution manifest that is hash-verified at construction
    before any HTTP call or audit append is made.

Ops10GroupExecutor
    Group-level executor for the OPS-10 contact-merge workflow.  Reads a
    separate group manifest (also hash-verified at construction), then for
    each group: validates live state against the manifest, PATCHes the
    survivor once, verifies the result, and then DELETEs candidates one at a
    time — each through its own intent/outcome audit pair.

Neither class performs a live create operation.  Neither reads env vars for
credentials or writes to certificate stores.

Invariants (from ``write_audit_log.py``, preserved here)
---------------------------------------------------------
``WriteAuditLogError.write_completed`` is ``False`` → safe to retry.
``WriteAuditOutcomeLogError.write_completed`` is ``True`` → never retry.
A destination write never begins before its intent line is fsync'd.
A write-completed outcome-log failure is never retried automatically.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from tools.msgraph_write_client import (
    DEFAULT_GRAPH_BASE_URL,
    TokenProvider,
    _AuditedGraphWriteClient,
)
from tools.sync_json_http import (
    HttpRequestError,
    build_url,
    request_json,
    urllib_request,
)
from tools.write_audit_log import (
    MSGRAPH_CONTACTS,
    WriteAuditOutcomeLogError,
    WriteAuditRecorder,
    require_trigger,
)
import time
from datetime import datetime


# ── Error types ───────────────────────────────────────────────────────────────


class ManifestError(ValueError):
    """Manifest validation failed; no HTTP request or audit append has occurred."""


class ETagMismatchError(RuntimeError):
    """Live ETag does not match the manifest-approved ETag."""


class BeforeStateMismatchError(RuntimeError):
    """Live before-state hash does not match the manifest-approved hash."""


class VerificationError(RuntimeError):
    """Post-write independent verification failed; the write completed but cannot be confirmed."""


class PartialStateError(RuntimeError):
    """Group is in an uncertain partial state; explicit reconciliation required before retry."""


# ── Manifest helpers ──────────────────────────────────────────────────────────

#: Graph fields that are read-only or service-managed and must never appear in
#: managed_fields or patch payloads.
_IMMUTABLE_FIELDS: frozenset[str] = frozenset({
    "id",
    "@odata.etag",
    "changeKey",
    "createdDateTime",
    "lastModifiedDateTime",
    "parentFolderId",
})


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contact_state_hash(contact: Any) -> str:
    """Stable SHA-256 of a contact dict (keys sorted for canonical serialization)."""
    serialized = json.dumps(contact, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return _sha256_hex(serialized)


def _extract_etag(contact: Any) -> str:
    """Extract ``@odata.etag`` from a Graph contact response body."""
    if not isinstance(contact, dict):
        return ""
    return str(contact.get("@odata.etag") or "")


# ── User-contacts manifest ────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _ContactManifestEntry:
    contact_id: str
    operation: str  # "update" or "delete"
    approved_etag: str
    approved_before_hash: str
    payload: dict[str, Any] | None  # required for "update", None for "delete"
    managed_fields: tuple[str, ...] | None  # required for "update", None for "delete"
    trigger: str


def _load_contact_manifest(
    manifest_path: Path,
    expected_sha256: str,
    target_user: str,
) -> dict[str, _ContactManifestEntry]:
    """Read and validate a user-contacts manifest.

    Returns a mapping from ``contact_id`` to ``_ContactManifestEntry``.
    Raises :class:`ManifestError` on any validation failure — before any HTTP
    request or audit append.
    """
    if not manifest_path.is_file():
        raise ManifestError(f"Manifest file not found: {manifest_path}")

    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"Cannot read manifest file: {exc}") from exc

    actual_hash = _sha256_hex(raw_bytes)
    if actual_hash != expected_sha256.lower().strip():
        raise ManifestError(
            "Manifest SHA-256 does not match the expected value."
        )

    try:
        manifest = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError(f"Manifest is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ManifestError("Manifest root must be a JSON object.")

    if manifest.get("target_user", "") != target_user:
        raise ManifestError(
            "Manifest target_user does not match this client's target_user."
        )

    entries_raw = manifest.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise ManifestError("Manifest must contain a non-empty 'entries' list.")

    entries: dict[str, _ContactManifestEntry] = {}
    seen_ids: set[str] = set()

    for i, raw in enumerate(entries_raw):
        if not isinstance(raw, dict):
            raise ManifestError(f"Entry {i} is not a JSON object.")

        contact_id = str(raw.get("contact_id") or "").strip()
        if not contact_id:
            raise ManifestError(f"Entry {i} has a missing or blank contact_id.")

        if contact_id in seen_ids:
            raise ManifestError(f"Duplicate contact_id in manifest: {contact_id!r}")
        seen_ids.add(contact_id)

        operation = raw.get("operation", "")
        if operation not in ("update", "delete"):
            raise ManifestError(
                f"Entry {i} ({contact_id!r}) has unsupported operation {operation!r}; "
                "only 'update' and 'delete' are allowed."
            )

        approved_etag = str(raw.get("approved_etag") or "").strip()
        if not approved_etag:
            raise ManifestError(f"Entry {i} ({contact_id!r}) is missing approved_etag.")

        approved_before_hash = str(raw.get("approved_before_hash") or "").strip()
        if not approved_before_hash:
            raise ManifestError(f"Entry {i} ({contact_id!r}) is missing approved_before_hash.")

        trigger = str(raw.get("trigger") or "").strip()
        if not trigger:
            raise ManifestError(f"Entry {i} ({contact_id!r}) is missing trigger text.")

        payload: dict[str, Any] | None = None
        managed_fields: tuple[str, ...] | None = None

        if operation == "update":
            payload_raw = raw.get("payload")
            if not isinstance(payload_raw, dict):
                raise ManifestError(f"Entry {i} ({contact_id!r}) update entry must have a 'payload' dict.")
            payload = payload_raw

            mf_raw = raw.get("managed_fields")
            if not isinstance(mf_raw, list) or not mf_raw:
                raise ManifestError(f"Entry {i} ({contact_id!r}) update entry must have a non-empty 'managed_fields' list.")
            managed_fields = tuple(str(f) for f in mf_raw)

            # managed_fields must be unique.
            if len(managed_fields) != len(set(managed_fields)):
                raise ManifestError(
                    f"Entry {i} ({contact_id!r}) managed_fields contains duplicate field names."
                )

            # Immutable fields forbidden in managed_fields.
            bad_mf = [f for f in managed_fields if f in _IMMUTABLE_FIELDS]
            if bad_mf:
                raise ManifestError(
                    f"Entry {i} ({contact_id!r}) managed_fields contains immutable field(s): {bad_mf}."
                )

            # Payload keys must equal managed_fields exactly.
            if set(payload.keys()) != set(managed_fields):
                raise ManifestError(
                    f"Entry {i} ({contact_id!r}) payload keys do not exactly match managed_fields."
                )

            # Immutable fields forbidden in payload (redundant guard; belt-and-suspenders).
            bad_pl = [f for f in payload if f in _IMMUTABLE_FIELDS]
            if bad_pl:
                raise ManifestError(
                    f"Entry {i} ({contact_id!r}) payload contains immutable field(s): {bad_pl}."
                )

        entries[contact_id] = _ContactManifestEntry(
            contact_id=contact_id,
            operation=operation,
            approved_etag=approved_etag,
            approved_before_hash=approved_before_hash,
            payload=payload,
            managed_fields=managed_fields,
            trigger=trigger,
        )

    return entries


# ── MicrosoftGraphUserContactsWriteClient ────────────────────────────────────


class MicrosoftGraphUserContactsWriteClient(_AuditedGraphWriteClient):
    """Audited app-only update/delete on ``/users/{target_user}/contacts/{id}``.

    *manifest_path* must be an approved execution manifest whose SHA-256 matches
    *manifest_sha256* (both are validated at construction, before any HTTP call).

    *target_user* is URL-encoded for safe use as a path segment.

    This class does NOT provide a create operation — ``create_contact`` from
    :class:`MicrosoftGraphContactsWriteClient` remains unaffected.
    """

    DESTINATION = MSGRAPH_CONTACTS
    ACTOR = "msgraph_user_contacts_write_client"

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        target_user: str,
        manifest_path: str | Path,
        manifest_sha256: str,
        **kwargs: Any,
    ) -> None:
        if not isinstance(target_user, str) or not target_user.strip():
            raise ValueError(
                "MicrosoftGraphUserContactsWriteClient requires a non-blank target_user."
            )
        self.target_user = target_user.strip()
        self._encoded_user = urllib.parse.quote(self.target_user, safe="")

        super().__init__(token_provider, **kwargs)

        # Validate manifest at construction — before any HTTP or audit activity.
        self._manifest = _load_contact_manifest(
            Path(manifest_path), manifest_sha256, self.target_user
        )

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _user_contact_path(self, contact_id: str) -> str:
        return f"/users/{self._encoded_user}/contacts/{contact_id}"

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _call_if_match(
        self, method: str, path: str, etag: str, *, json_body: Any = None
    ) -> Any:
        """Like ``_call`` but includes an ``If-Match`` header.

        Forces ``max_retries=0`` per spec: exactly one HTTP attempt for every
        PATCH and DELETE.  A timeout, connection loss, or retryable status after
        mutation leaves an intent without outcome and requires explicit
        reconciliation.
        """
        headers = {**self._headers(), "If-Match": etag}
        return request_json(
            self.request_fn,
            method,
            build_url(self.base_url, path),
            headers=headers,
            json_body=json_body,
            timeout=self.timeout,
            max_retries=0,  # exactly one attempt for mutations
            sleep=self.sleep,
        )

    # ── Manifest lookup and state verification ────────────────────────────────

    def _get_manifest_entry(self, contact_id: str, operation: str) -> _ContactManifestEntry:
        entry = self._manifest.get(contact_id)
        if entry is None:
            raise ManifestError(
                f"contact_id {contact_id!r} is not in the approved manifest."
            )
        if entry.operation != operation:
            raise ManifestError(
                f"contact_id {contact_id!r} is approved for {entry.operation!r} "
                f"but {operation!r} was requested."
            )
        return entry

    def _verify_live_contact(
        self, contact_id: str, live_state: Any, entry: _ContactManifestEntry
    ) -> None:
        """Assert live ETag and before-state hash match the manifest.

        Raises :class:`ETagMismatchError` or :class:`BeforeStateMismatchError`
        before any audit append or destination write.
        """
        live_etag = _extract_etag(live_state)
        if not live_etag:
            raise ETagMismatchError(
                f"Contact {contact_id!r} GET returned no @odata.etag field."
            )
        if live_etag != entry.approved_etag:
            raise ETagMismatchError(
                f"Contact {contact_id!r} live ETag does not match the approved ETag."
            )
        live_hash = _contact_state_hash(live_state)
        if live_hash != entry.approved_before_hash:
            raise BeforeStateMismatchError(
                f"Contact {contact_id!r} before-state hash does not match the approved hash."
            )

    # ── Write operations ──────────────────────────────────────────────────────

    def update_contact(self, contact_id: str, *, trigger: str) -> Any:
        """Audited ``PATCH /users/{user}/contacts/{id}``.

        The payload, managed fields, and approved ETag all come from the
        manifest — no caller-supplied payload is accepted.

        Sequence::

            validate manifest entry
            require_trigger(trigger)
            GET before
            compare ETag + before-state hash to manifest
            fsync intent audit line
            PATCH with If-Match
            independent GET verification of all managed fields
            fsync outcome audit line

        Never retries after the destination PATCH; raises
        :class:`VerificationError` if the independent GET fails or if any
        managed field does not match the manifest payload.
        """
        require_trigger(trigger)
        entry = self._get_manifest_entry(contact_id, "update")

        path = self._user_contact_path(contact_id)
        before = self._call("GET", path)
        self._verify_live_contact(contact_id, before, entry)

        authorized = self._authorize(
            operation="update",
            record_id=contact_id,
            before=before,
            trigger=trigger,
        )

        assert entry.payload is not None
        self._call_if_match("PATCH", path, entry.approved_etag, json_body=entry.payload)

        # Independent GET verification — must succeed before outcome is recorded.
        try:
            after = self._call("GET", path)
        except (HttpRequestError, OSError) as exc:
            authorized.record_outcome(after_fetch_failed=True)
            raise VerificationError(
                f"Post-PATCH GET of contact {contact_id!r} failed."
            ) from exc

        # Managed-field verification — all fields must exactly match the manifest payload.
        assert entry.managed_fields is not None
        mismatched = [
            f for f in entry.managed_fields
            if (after.get(f) if isinstance(after, dict) else None) != entry.payload.get(f)
        ]
        if mismatched:
            authorized.record_outcome(after=after)
            raise VerificationError(
                f"Post-PATCH verification failed for contact {contact_id!r}: "
                f"managed field(s) {mismatched} do not match the manifest payload."
            )

        authorized.record_outcome(after=after)
        return after

    def delete_contact(self, contact_id: str, *, trigger: str) -> Any:
        """Audited ``DELETE /users/{user}/contacts/{id}``.

        Sequence::

            validate manifest entry
            require_trigger(trigger)
            GET before
            compare ETag + before-state hash to manifest
            fsync intent audit line
            DELETE with If-Match
            independent GET verification (must return 404)
            fsync outcome audit line

        Raises :class:`VerificationError` if the post-DELETE GET does not
        return 404.  Never retries after the destination DELETE.
        """
        require_trigger(trigger)
        entry = self._get_manifest_entry(contact_id, "delete")

        path = self._user_contact_path(contact_id)
        before = self._call("GET", path)
        self._verify_live_contact(contact_id, before, entry)

        authorized = self._authorize(
            operation="delete",
            record_id=contact_id,
            before=before,
            trigger=trigger,
        )

        self._call_if_match("DELETE", path, entry.approved_etag)

        # Independent 404 verification.
        try:
            self._call("GET", path)
            # Still returns 200 — something went wrong.
            authorized.record_outcome(after=before, after_fetch_failed=True)
            raise VerificationError(
                f"Contact {contact_id!r} still returns 200 after DELETE."
            )
        except HttpRequestError as exc:
            if exc.status_code == 404:
                pass  # Expected — contact is gone.
            else:
                authorized.record_outcome(after=None, after_fetch_failed=True)
                raise VerificationError(
                    f"Post-DELETE GET of contact {contact_id!r} returned unexpected "
                    f"status {exc.status_code}."
                ) from exc
        except OSError as exc:
            authorized.record_outcome(after=None, after_fetch_failed=True)
            raise VerificationError(
                f"Post-DELETE GET of contact {contact_id!r} failed with OS error."
            ) from exc

        authorized.record_outcome(after=None)
        return before


# ── Group manifest (Ops10GroupExecutor) ───────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _GroupEntry:
    group_id: str
    survivor_id: str
    candidate_ids: tuple[str, ...]
    approved_before_hashes: dict[str, str]
    approved_etags: dict[str, str]
    desired_survivor_payload: dict[str, Any]
    managed_fields: tuple[str, ...]
    candidate_delete_order: tuple[str, ...]
    trigger: str


def _load_group_manifest(
    manifest_path: Path,
    expected_sha256: str,
    target_user: str,
) -> tuple[str, list[_GroupEntry]]:
    """Read and validate a group manifest.

    Returns ``(manifest_id, [_GroupEntry, ...])`` or raises
    :class:`ManifestError`.
    """
    if not manifest_path.is_file():
        raise ManifestError(f"Group manifest file not found: {manifest_path}")

    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"Cannot read group manifest: {exc}") from exc

    actual_hash = _sha256_hex(raw_bytes)
    if actual_hash != expected_sha256.lower().strip():
        raise ManifestError("Group manifest SHA-256 does not match the expected value.")

    try:
        manifest = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError(f"Group manifest is not valid JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ManifestError("Group manifest root must be a JSON object.")

    manifest_id = str(manifest.get("manifest_id") or "").strip()
    if not manifest_id:
        raise ManifestError("Group manifest is missing manifest_id.")

    if manifest.get("target_user", "") != target_user:
        raise ManifestError("Group manifest target_user does not match this executor's target_user.")

    groups_raw = manifest.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ManifestError("Group manifest must contain a non-empty 'groups' list.")

    groups: list[_GroupEntry] = []
    seen_group_ids: set[str] = set()
    seen_contact_ids: set[str] = set()

    for gi, raw in enumerate(groups_raw):
        if not isinstance(raw, dict):
            raise ManifestError(f"Group {gi} is not a JSON object.")

        group_id = str(raw.get("group_id") or "").strip()
        if not group_id:
            raise ManifestError(f"Group {gi} is missing group_id.")
        if group_id in seen_group_ids:
            raise ManifestError(f"Duplicate group_id in manifest: {group_id!r}")
        seen_group_ids.add(group_id)

        survivor_id = str(raw.get("survivor_id") or "").strip()
        if not survivor_id:
            raise ManifestError(f"Group {gi} ({group_id!r}) is missing survivor_id.")

        candidate_ids_raw = raw.get("candidate_ids")
        if not isinstance(candidate_ids_raw, list) or not candidate_ids_raw:
            raise ManifestError(f"Group {gi} ({group_id!r}) must have a non-empty candidate_ids list.")
        candidate_ids = tuple(str(c).strip() for c in candidate_ids_raw)

        # All IDs in the group must be unique within the group.
        all_ids_in_group = [survivor_id] + list(candidate_ids)
        if len(set(all_ids_in_group)) != len(all_ids_in_group):
            raise ManifestError(f"Group {gi} ({group_id!r}) contains duplicate contact IDs.")

        # All IDs must be globally unique across all groups.
        for cid in all_ids_in_group:
            if cid in seen_contact_ids:
                raise ManifestError(
                    f"Contact ID {cid!r} appears in multiple groups in the manifest."
                )
            seen_contact_ids.add(cid)

        approved_before_hashes_raw = raw.get("approved_before_hashes")
        if not isinstance(approved_before_hashes_raw, dict):
            raise ManifestError(f"Group {gi} ({group_id!r}) is missing approved_before_hashes dict.")
        all_ids_set = set(all_ids_in_group)
        for cid in all_ids_in_group:
            if cid not in approved_before_hashes_raw:
                raise ManifestError(
                    f"Group {gi} ({group_id!r}) is missing approved_before_hash for {cid!r}."
                )
        # Reject extra keys — the dict must be exactly the survivor + candidate IDs.
        extra_bh = set(approved_before_hashes_raw.keys()) - all_ids_set
        if extra_bh:
            raise ManifestError(
                f"Group {gi} ({group_id!r}) approved_before_hashes has extra ID(s): {sorted(extra_bh)}."
            )
        approved_before_hashes = {k: str(v) for k, v in approved_before_hashes_raw.items()}

        approved_etags_raw = raw.get("approved_etags")
        if not isinstance(approved_etags_raw, dict):
            raise ManifestError(f"Group {gi} ({group_id!r}) is missing approved_etags dict.")
        for cid in all_ids_in_group:
            if cid not in approved_etags_raw:
                raise ManifestError(
                    f"Group {gi} ({group_id!r}) is missing approved_etag for {cid!r}."
                )
        # Reject extra keys.
        extra_et = set(approved_etags_raw.keys()) - all_ids_set
        if extra_et:
            raise ManifestError(
                f"Group {gi} ({group_id!r}) approved_etags has extra ID(s): {sorted(extra_et)}."
            )
        approved_etags = {k: str(v) for k, v in approved_etags_raw.items()}

        desired_payload = raw.get("desired_survivor_payload")
        if not isinstance(desired_payload, dict):
            raise ManifestError(f"Group {gi} ({group_id!r}) must have a 'desired_survivor_payload' dict.")

        mf_raw = raw.get("managed_fields")
        if not isinstance(mf_raw, list) or not mf_raw:
            raise ManifestError(f"Group {gi} ({group_id!r}) must have a non-empty 'managed_fields' list.")
        managed_fields = tuple(str(f) for f in mf_raw)

        # managed_fields must be unique.
        if len(managed_fields) != len(set(managed_fields)):
            raise ManifestError(
                f"Group {gi} ({group_id!r}) managed_fields contains duplicate field names."
            )

        # Immutable fields forbidden in managed_fields.
        bad_mf = [f for f in managed_fields if f in _IMMUTABLE_FIELDS]
        if bad_mf:
            raise ManifestError(
                f"Group {gi} ({group_id!r}) managed_fields contains immutable field(s): {bad_mf}."
            )

        # Payload keys must equal managed_fields exactly.
        if set(desired_payload.keys()) != set(managed_fields):
            raise ManifestError(
                f"Group {gi} ({group_id!r}) desired_survivor_payload keys do not exactly match managed_fields."
            )

        # Immutable fields forbidden in payload (belt-and-suspenders).
        bad_pl = [f for f in desired_payload if f in _IMMUTABLE_FIELDS]
        if bad_pl:
            raise ManifestError(
                f"Group {gi} ({group_id!r}) desired_survivor_payload contains immutable field(s): {bad_pl}."
            )

        del_order_raw = raw.get("candidate_delete_order")
        if not isinstance(del_order_raw, list):
            raise ManifestError(f"Group {gi} ({group_id!r}) must have a 'candidate_delete_order' list.")
        candidate_delete_order = tuple(str(c) for c in del_order_raw)
        if set(candidate_delete_order) != set(candidate_ids):
            raise ManifestError(
                f"Group {gi} ({group_id!r}) candidate_delete_order does not match candidate_ids."
            )

        trigger = str(raw.get("trigger") or "").strip()
        if not trigger:
            raise ManifestError(f"Group {gi} ({group_id!r}) is missing trigger text.")

        groups.append(
            _GroupEntry(
                group_id=group_id,
                survivor_id=survivor_id,
                candidate_ids=candidate_ids,
                approved_before_hashes=approved_before_hashes,
                approved_etags=approved_etags,
                desired_survivor_payload=desired_payload,
                managed_fields=managed_fields,
                candidate_delete_order=candidate_delete_order,
                trigger=trigger,
            )
        )

    return manifest_id, groups


# ── Ops10GroupExecutor ────────────────────────────────────────────────────────


class Ops10GroupExecutor(_AuditedGraphWriteClient):
    """Group-level executor for the OPS-10 contact-merge workflow.

    For each group in the approved manifest:

    1. Validate the entire group against the manifest.
    2. GET the survivor and every candidate; compare live ID, before-state hash,
       and ETag to the manifest.  Hard-stop on any drift.
    3. If the survivor already exactly matches the desired payload and every
       candidate exists with approved ETags, skip the PATCH (idempotency).
    4. PATCH the survivor with the manifest's fully precomputed payload; audit
       the PATCH through its own intent/outcome pair with ``If-Match``.
    5. Independently GET the survivor and verify every managed field.
    6. Delete no candidate unless survivor verification passes.
    7. DELETE candidates one at a time, each through its own intent/outcome
       audit pair with ``If-Match``; verify 404 after each.
    8. Stop on the first failure; never advance to the next group.

    Merge decisions (multi-value union, note combination, desired payload) are
    NOT computed here — the merged-state manifest stage does that.  This class
    only enforces and executes a separately approved manifest.
    """

    DESTINATION = MSGRAPH_CONTACTS
    ACTOR = "msgraph_ops10_group_executor"

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        target_user: str,
        manifest_path: str | Path,
        manifest_sha256: str,
        **kwargs: Any,
    ) -> None:
        if not isinstance(target_user, str) or not target_user.strip():
            raise ValueError("Ops10GroupExecutor requires a non-blank target_user.")
        self.target_user = target_user.strip()
        self._encoded_user = urllib.parse.quote(self.target_user, safe="")

        super().__init__(token_provider, **kwargs)

        self._manifest_id, self._groups = _load_group_manifest(
            Path(manifest_path), manifest_sha256, self.target_user
        )
        self._group_index: dict[str, _GroupEntry] = {g.group_id: g for g in self._groups}

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _contact_path(self, contact_id: str) -> str:
        return f"/users/{self._encoded_user}/contacts/{contact_id}"

    def _call_if_match(
        self, method: str, path: str, etag: str, *, json_body: Any = None
    ) -> Any:
        """Like ``_call`` but includes an ``If-Match`` header, exactly one attempt."""
        headers = {**self._headers(), "If-Match": etag}
        return request_json(
            self.request_fn,
            method,
            build_url(self.base_url, path),
            headers=headers,
            json_body=json_body,
            timeout=self.timeout,
            max_retries=0,  # exactly one attempt for mutations
            sleep=self.sleep,
        )

    # ── State comparison helpers ──────────────────────────────────────────────

    def _verify_live_contact_state(
        self, contact_id: str, live: Any, group: _GroupEntry
    ) -> None:
        """Raise on ETag or before-state hash mismatch for one contact."""
        if not isinstance(live, dict) or live.get("id") != contact_id:
            raise VerificationError(
                f"Contact {contact_id!r} GET returned unexpected id."
            )
        live_etag = _extract_etag(live)
        if not live_etag:
            raise ETagMismatchError(
                f"Contact {contact_id!r} GET returned no @odata.etag."
            )
        approved_etag = group.approved_etags.get(contact_id, "")
        if live_etag != approved_etag:
            raise ETagMismatchError(
                f"Contact {contact_id!r} live ETag does not match the approved ETag."
            )
        live_hash = _contact_state_hash(live)
        approved_hash = group.approved_before_hashes.get(contact_id, "")
        if live_hash != approved_hash:
            raise BeforeStateMismatchError(
                f"Contact {contact_id!r} before-state hash does not match the approved hash."
            )

    def _survivor_matches_desired(self, live_survivor: Any, group: _GroupEntry) -> bool:
        """True iff all managed fields in the live survivor match the desired payload."""
        if not isinstance(live_survivor, dict):
            return False
        return all(
            live_survivor.get(f) == group.desired_survivor_payload.get(f)
            for f in group.managed_fields
        )

    def _verify_survivor_managed_fields(self, live: Any, group: _GroupEntry) -> None:
        """Raise VerificationError if any managed field does not match the desired payload."""
        if not isinstance(live, dict):
            raise VerificationError("Survivor GET did not return a dict.")
        mismatched = [
            f for f in group.managed_fields
            if live.get(f) != group.desired_survivor_payload.get(f)
        ]
        if mismatched:
            raise VerificationError(
                f"Survivor post-PATCH managed field(s) {mismatched} do not match "
                "the desired payload."
            )

    def _verify_404(self, contact_id: str) -> None:
        """Raise VerificationError unless GET returns 404."""
        path = self._contact_path(contact_id)
        try:
            self._call("GET", path)
            raise VerificationError(
                f"Candidate {contact_id!r} still returns 200 after DELETE."
            )
        except HttpRequestError as exc:
            if exc.status_code != 404:
                raise VerificationError(
                    f"Candidate {contact_id!r} DELETE verification returned unexpected "
                    f"status {exc.status_code}."
                ) from exc
        except OSError as exc:
            raise VerificationError(
                f"Candidate {contact_id!r} DELETE verification GET failed with OS error."
            ) from exc

    def _check_no_incomplete_intents(self, contact_ids: list[str]) -> None:
        """Hard-stop before any HTTP I/O if any intent lacks a matching outcome.

        Scans the existing audit log for the destination and every contact ID in
        the group.  An intent audit ID with no corresponding outcome ID means a
        prior run left an uncertain state; explicit reconciliation is required
        before any retry.  OPS-17 never infers completion from live state.
        """
        from tools.write_audit_log import PHASE_INTENT, PHASE_OUTCOME, iter_entries

        log_dir = self.recorder.log_dir
        if log_dir is None or not log_dir.is_dir():
            return  # No audit log yet — nothing to check.

        contact_id_set = set(contact_ids)
        intents: dict[str, Any] = {}   # audit_id -> record_id
        outcomes: set[str] = set()     # audit_ids with a recorded outcome

        for _, _, entry in iter_entries(log_dir):
            if entry.get("destination") != self.recorder.destination:
                continue
            record_id = entry.get("record_id")
            if record_id not in contact_id_set:
                continue
            audit_id = entry.get("audit_id", "")
            phase = entry.get("audit_phase")
            if phase == PHASE_INTENT:
                intents[audit_id] = record_id
            elif phase == PHASE_OUTCOME:
                outcomes.add(audit_id)

        incomplete = {aid: rid for aid, rid in intents.items() if aid not in outcomes}
        if incomplete:
            orphan_ids = sorted(set(incomplete.values()))
            raise PartialStateError(
                f"Found intent(s) without outcome for contact ID(s) {orphan_ids}: "
                "explicit reconciliation required before any retry. "
                "OPS-17 does not infer completion from current state."
            )

    # ── Public interface ──────────────────────────────────────────────────────

    def execute_all(self) -> list[str]:
        """Execute every group in the manifest in order.  Returns completed group IDs.

        Stops on the first failure — never advances to the next group after a
        partial or uncertain group.
        """
        completed: list[str] = []
        for group in self._groups:
            self.execute_group(group.group_id)
            completed.append(group.group_id)
        return completed

    def execute_group(self, group_id: str) -> None:
        """Execute one approved group identified by *group_id*.

        Raises :class:`ManifestError`, :class:`ETagMismatchError`,
        :class:`BeforeStateMismatchError`, :class:`VerificationError`, or
        :class:`PartialStateError` on any failure — and stops immediately.
        """
        group = self._group_index.get(group_id)
        if group is None:
            raise ManifestError(f"Group {group_id!r} is not in the approved manifest.")

        require_trigger(group.trigger)

        all_ids = [group.survivor_id] + list(group.candidate_ids)

        # ── Pre-flight: scan audit log for incomplete intents before any HTTP. ──
        self._check_no_incomplete_intents(all_ids)

        survivor_path = self._contact_path(group.survivor_id)

        # ── Step 1: GET survivor and all candidates; verify against manifest. ──

        live_contacts: dict[str, Any] = {}

        live_survivor = self._call("GET", survivor_path)
        self._verify_live_contact_state(group.survivor_id, live_survivor, group)
        live_contacts[group.survivor_id] = live_survivor

        absent_candidates: set[str] = set()
        for cid in group.candidate_ids:
            try:
                live = self._call("GET", self._contact_path(cid))
                self._verify_live_contact_state(cid, live, group)
                live_contacts[cid] = live
            except HttpRequestError as exc:
                if exc.status_code == 404:
                    absent_candidates.add(cid)
                else:
                    raise PartialStateError(
                        f"Candidate {cid!r} GET returned unexpected status {exc.status_code}."
                    ) from exc

        # ── Step 2: Idempotency and partial-state checks. ─────────────────────

        survivor_already_correct = self._survivor_matches_desired(live_survivor, group)

        if absent_candidates:
            if not survivor_already_correct:
                raise PartialStateError(
                    f"Group {group_id!r} has absent candidates "
                    f"{sorted(absent_candidates)} but the survivor does not yet match "
                    "the desired payload — uncertain partial state."
                )
            # Survivor is correct; some candidates already gone — those are no-ops.

        # ── Step 3: PATCH survivor (skip if already correct). ─────────────────

        if not survivor_already_correct:
            authorized_patch = self._authorize(
                operation="update",
                record_id=group.survivor_id,
                before=live_survivor,
                trigger=group.trigger,
            )

            self._call_if_match(
                "PATCH",
                survivor_path,
                group.approved_etags[group.survivor_id],
                json_body=group.desired_survivor_payload,
            )

            # ── Step 4: Independent GET + managed-field verification. ──────────

            try:
                live_survivor_after = self._call("GET", survivor_path)
            except (HttpRequestError, OSError) as exc:
                authorized_patch.record_outcome(after_fetch_failed=True)
                raise VerificationError(
                    f"Group {group_id!r}: survivor GET after PATCH failed."
                ) from exc

            self._verify_survivor_managed_fields(live_survivor_after, group)
            authorized_patch.record_outcome(after=live_survivor_after)
        else:
            # Survivor already matches; independently confirm managed fields are correct.
            self._verify_survivor_managed_fields(live_survivor, group)

        # ── Step 5: DELETE candidates one at a time. ──────────────────────────
        # No candidate is deleted unless the above verification has passed.

        for cid in group.candidate_delete_order:
            if cid in absent_candidates:
                # Independently verified no-op — survivor already correct, candidate gone.
                continue

            candidate_before = live_contacts[cid]

            authorized_del = self._authorize(
                operation="delete",
                record_id=cid,
                before=candidate_before,
                trigger=group.trigger,
            )

            self._call_if_match(
                "DELETE",
                self._contact_path(cid),
                group.approved_etags[cid],
            )

            # Verify 404.
            try:
                self._verify_404(cid)
            except VerificationError:
                authorized_del.record_outcome(after=None, after_fetch_failed=True)
                raise

            authorized_del.record_outcome(after=None)


__all__ = [
    "BeforeStateMismatchError",
    "ETagMismatchError",
    "ManifestError",
    "MicrosoftGraphUserContactsWriteClient",
    "Ops10GroupExecutor",
    "PartialStateError",
    "VerificationError",
]
