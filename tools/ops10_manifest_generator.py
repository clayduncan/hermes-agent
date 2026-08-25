"""OPS-10 Merged-State Manifest Generator.

Converts the exact-parity OPS-10 pre-write snapshot and item reconciliation
into:
  1. An executor-ready final merged-state manifest for 84 groups / 87 candidates.
  2. A redacted human review artifact.

No network access, no contact mutation, no inference outside Clay's approved rules.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

# ── Version / revision constants ──────────────────────────────────────────────

GENERATOR_VERSION = "1.0.0"
DECISION_REVISION = "r1"

# ── Expected data counts ──────────────────────────────────────────────────────

EXPECTED_SNAPSHOT_COUNT = 4381
EXPECTED_KEEP_GROUPS = 84
EXPECTED_DELETE_CANDIDATES = 87
EXPECTED_HOLD_RECORDS = 166
EXPECTED_IN_SCOPE_IDS = 171          # 84 survivors + 87 candidates
EXPECTED_NOTE_DECISIONS_BEFORE = 2   # groups needing note merge before rule applied
EXPECTED_NOTE_DECISIONS_AFTER = 0    # groups remaining after rule applied

# ── Executor source identity ──────────────────────────────────────────────────

EXECUTOR_COMMIT = "32652c04defd33feae2c5deab341e691ff7e3a4a"
EXPECTED_EXECUTOR_SHA256 = (
    "507ce1827df29510740440641b7a8d3eea8f16aef125159abf2309b277d446b8"
)

# ── Immutable (service-managed) fields ───────────────────────────────────────

_IMMUTABLE_FIELDS: frozenset[str] = frozenset({
    "id",
    "@odata.etag",
    "changeKey",
    "createdDateTime",
    "lastModifiedDateTime",
    "parentFolderId",
})

# ── Mutable contact allowlist ─────────────────────────────────────────────────

MUTABLE_FIELDS: tuple[str, ...] = (
    "assistantName",
    "birthday",
    "businessAddress",
    "businessHomePage",
    "businessPhones",
    "categories",
    "children",
    "companyName",
    "department",
    "displayName",
    "emailAddresses",
    "fileAs",
    "generation",
    "givenName",
    "homeAddress",
    "homePhones",
    "imAddresses",
    "initials",
    "jobTitle",
    "manager",
    "middleName",
    "mobilePhone",
    "nickName",
    "officeLocation",
    "otherAddress",
    "personalNotes",
    "primaryEmailAddress",
    "profession",
    "secondaryEmailAddress",
    "spouseName",
    "surname",
    "tertiaryEmailAddress",
    "title",
    "yomiCompanyName",
    "yomiGivenName",
    "yomiSurname",
)
MUTABLE_FIELDS_SET: frozenset[str] = frozenset(MUTABLE_FIELDS)

# Multi-value fields: unioned survivor-first.
MULTI_VALUE_FIELDS: frozenset[str] = frozenset({
    "categories",
    "children",
    "businessPhones",
    "homePhones",
    "imAddresses",
    "emailAddresses",
})
_PHONE_MULTI_FIELDS: frozenset[str] = frozenset({"businessPhones", "homePhones"})
_EMAIL_MULTI_FIELDS: frozenset[str] = frozenset({"emailAddresses"})

# Structured address fields.
ADDRESS_FIELDS: frozenset[str] = frozenset({
    "businessAddress",
    "homeAddress",
    "otherAddress",
})
ADDRESS_SUBFIELDS: tuple[str, ...] = (
    "street",
    "city",
    "state",
    "postalCode",
    "countryOrRegion",
)

# Explicit scalar override decision names — exact real reconciliation normalizedName keys.
# Never use Title Case display names; match is case-sensitive on the lowercase norm keys.
# adam o'daniel uses curly apostrophe U+2019 as it appears in the real reconciliation.
EXPLICIT_DECISION_NAMES: frozenset[str] = frozenset({
    "aden eppolito",
    "chris reickard",
    "chris ferrari",
})
_TWO_CANDIDATE_NAMES: frozenset[str] = frozenset({
    "aaron kimball",
    "adam o’daniel",  # curly apostrophe U+2019
})

# ── Error types ───────────────────────────────────────────────────────────────


class GeneratorError(ValueError):
    """Manifest generator validation or merge error."""


class ScalarConflict(GeneratorError):
    """Unresolvable scalar field conflict (fail-closed)."""

    def __init__(self, field: str, group_name: str) -> None:
        super().__init__(
            f"Scalar conflict on field {field!r} for group {group_name!r}. "
            "No approved resolution exists for this collision."
        )
        self.field = field
        self.group_name = group_name


class AddressConflict(GeneratorError):
    """Nonblank address subfield conflict (fail-closed, no raw values emitted)."""

    def __init__(self, subfield_path: str, group_name: str) -> None:
        super().__init__(
            f"Address conflict on subfield {subfield_path!r} for group {group_name!r}."
        )
        self.subfield_path = subfield_path
        self.group_name = group_name


# ── Normalization helpers ─────────────────────────────────────────────────────


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s)


def _normalize_email_addr(s: str) -> str:
    return s.strip().lower()


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and not value:
        return True
    return False


def _normalize_scalar_for_cmp(field: str, value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if field == "mobilePhone":
        return _digits_only(s)
    return s


# ── Multi-value field unions ──────────────────────────────────────────────────


def _union_phone_list(survivor: list, candidate: list) -> list:
    """Union phone lists. Digit-normalized dedup; preserve survivor formatting."""
    seen: set[str] = set()
    result: list = []
    for phone in survivor:
        d = _digits_only(str(phone))
        if d not in seen:
            seen.add(d)
            result.append(phone)
    for phone in candidate:
        d = _digits_only(str(phone))
        if d not in seen:
            seen.add(d)
            result.append(phone)
    return result


def _union_email_list(survivor: list, candidate: list) -> list:
    """Union email lists. Normalized-address dedup; preserve survivor object."""
    seen: set[str] = set()
    result: list = []
    for item in survivor:
        addr = _normalize_email_addr(
            item.get("address", "") if isinstance(item, dict) else str(item)
        )
        if addr not in seen:
            seen.add(addr)
            result.append(item)
    for item in candidate:
        addr = _normalize_email_addr(
            item.get("address", "") if isinstance(item, dict) else str(item)
        )
        if addr not in seen:
            seen.add(addr)
            result.append(item)
    return result


def _union_string_list(survivor: list, candidate: list) -> list:
    """Union string lists. Trim-normalized dedup; preserve first-seen original."""
    seen: set[str] = set()
    result: list = []
    for item in survivor:
        key = str(item).strip()
        if key not in seen:
            seen.add(key)
            result.append(item)
    for item in candidate:
        key = str(item).strip()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _union_multi_value(field: str, survivor_val: Any, candidate_val: Any) -> list:
    s_list = survivor_val if isinstance(survivor_val, list) else []
    c_list = candidate_val if isinstance(candidate_val, list) else []
    if field in _PHONE_MULTI_FIELDS:
        return _union_phone_list(s_list, c_list)
    if field in _EMAIL_MULTI_FIELDS:
        return _union_email_list(s_list, c_list)
    return _union_string_list(s_list, c_list)


# ── Personal notes merge ──────────────────────────────────────────────────────


def _merge_notes(survivor_notes: Any, candidate_notes_list: list[Any]) -> Any:
    """Merge personal notes: survivor body first, distinct candidate bodies in order.

    Separator: exactly two newline characters. No labels, provenance, or
    invented text.  Equality tested on stripped body; originals preserved.
    """

    def _norm(n: Any) -> str:
        return str(n).strip() if n is not None else ""

    seen: set[str] = set()
    parts: list[str] = []

    s_norm = _norm(survivor_notes)
    if s_norm:
        seen.add(s_norm)
        parts.append(str(survivor_notes))

    for cand_n in candidate_notes_list:
        c_norm = _norm(cand_n)
        if c_norm and c_norm not in seen:
            seen.add(c_norm)
            parts.append(str(cand_n))

    if not parts:
        return None
    return "\n\n".join(parts)


def _count_note_decision(survivor_notes: Any, candidate_notes_list: list[Any]) -> int:
    """Return 1 if this group requires a note merge decision, else 0.

    Two cases count as a decision:
    - Propagation: survivor note is blank and at least one candidate note is nonblank.
    - Conflict: survivor note is nonblank and at least one candidate note differs from it.
    """

    def _norm(n: Any) -> str:
        return str(n).strip() if n is not None else ""

    sv_norm = _norm(survivor_notes)
    nonblank_cands = {_norm(n) for n in candidate_notes_list if _norm(n)}

    if not sv_norm and nonblank_cands:
        # Propagation decision: blank survivor, nonblank candidates
        return 1
    if sv_norm and (nonblank_cands - {sv_norm}):
        # Conflict decision: survivor note differs from at least one candidate
        return 1
    return 0


# ── Address merge ─────────────────────────────────────────────────────────────


def _merge_address(
    field: str,
    survivor_val: Any,
    candidate_val: Any,
    group_name: str,
) -> Any:
    """Merge one structured address field. Raises AddressConflict on nonblank subfield mismatch."""
    if _is_blank(survivor_val) and _is_blank(candidate_val):
        return None
    if _is_blank(survivor_val):
        return candidate_val
    if _is_blank(candidate_val):
        return survivor_val
    if not isinstance(survivor_val, dict):
        return survivor_val
    if not isinstance(candidate_val, dict):
        return survivor_val

    merged = dict(survivor_val)
    for sub in ADDRESS_SUBFIELDS:
        sv = (survivor_val.get(sub) or "").strip()
        cv = (candidate_val.get(sub) or "").strip()
        if not sv and cv:
            merged[sub] = candidate_val[sub]
        elif sv and cv and sv != cv:
            raise AddressConflict(f"{field}.{sub}", group_name)
    return merged


# ── Scalar merge ──────────────────────────────────────────────────────────────


def _merge_scalar(
    field: str,
    survivor_val: Any,
    candidate_val: Any,
    group_name: str,
    explicit_candidate_wins: list[str],
    is_chris_ferrari: bool,
) -> Any:
    """Merge one scalar field.  Raises ScalarConflict on unresolvable collision."""
    # Chris Ferrari: always preserve survivor primaryEmailAddress object.
    if is_chris_ferrari and field == "primaryEmailAddress":
        return survivor_val

    # Explicit decision: candidate value wins for specified fields.
    if field in explicit_candidate_wins:
        return candidate_val if not _is_blank(candidate_val) else survivor_val

    s_blank = _is_blank(survivor_val)
    c_blank = _is_blank(candidate_val)

    if s_blank and c_blank:
        return None
    if s_blank:
        return candidate_val
    if c_blank:
        return survivor_val

    # Both nonblank — check normalized equality.
    s_norm = _normalize_scalar_for_cmp(field, survivor_val)
    c_norm = _normalize_scalar_for_cmp(field, candidate_val)

    if s_norm == c_norm:
        # Normalized-equal: preserve survivor formatting (covers mobilePhone explicitly).
        return survivor_val

    raise ScalarConflict(field, group_name)


# ── Contact merge ─────────────────────────────────────────────────────────────


def _merge_contact(
    group_name: str,
    survivor: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Apply Clay's approved merge rules.

    Returns (merged_payload, note_decision_count).
    merged_payload contains exactly MUTABLE_FIELDS keys.
    note_decision_count is 1 if this group had distinct nonblank note bodies, else 0.
    """
    explicit_candidate_wins: list[str] = []
    is_chris_ferrari = group_name == "chris ferrari"

    if group_name == "aden eppolito":
        explicit_candidate_wins = ["displayName", "surname", "fileAs"]
    elif group_name == "chris reickard":
        explicit_candidate_wins = ["displayName", "givenName", "fileAs"]
    # chris ferrari: fill blank fileAs from candidate is covered by default scalar rule.

    # Seed with survivor's mutable values.
    merged: dict[str, Any] = {field: survivor.get(field) for field in MUTABLE_FIELDS}

    # Apply each candidate in order (sorted by caller).
    for cand in candidates:
        for field in MUTABLE_FIELDS:
            if field == "personalNotes":
                continue  # handled below, all candidates at once

            sv = merged[field]
            cv = cand.get(field)

            if field in MULTI_VALUE_FIELDS:
                merged[field] = _union_multi_value(field, sv, cv)
            elif field in ADDRESS_FIELDS:
                merged[field] = _merge_address(field, sv, cv, group_name)
            else:
                merged[field] = _merge_scalar(
                    field, sv, cv, group_name,
                    explicit_candidate_wins, is_chris_ferrari,
                )

    # Personal notes: merge all candidates simultaneously to maintain order.
    sv_notes = survivor.get("personalNotes")
    cand_notes = [cand.get("personalNotes") for cand in candidates]
    note_decision_count = _count_note_decision(sv_notes, cand_notes)
    merged["personalNotes"] = _merge_notes(sv_notes, cand_notes)

    return merged, note_decision_count


# ── Executor source verification ──────────────────────────────────────────────


def _verify_executor_source(
    executor_source_path: Path,
    expected_sha256: str = EXPECTED_EXECUTOR_SHA256,
) -> ModuleType:
    """SHA-256-verify the executor source file then load it via importlib.

    Never imports from sys.modules or the active working tree implicitly.
    Raises GeneratorError on any verification failure.
    """
    if not executor_source_path.is_file():
        raise GeneratorError(
            f"Executor source not found: {executor_source_path}"
        )

    try:
        raw = executor_source_path.read_bytes()
    except OSError as exc:
        raise GeneratorError(f"Cannot read executor source: {exc}") from exc

    actual = _sha256_hex(raw)
    if actual != expected_sha256.lower().strip():
        raise GeneratorError(
            f"Executor source SHA-256 mismatch.\n"
            f"  Expected (commit {EXECUTOR_COMMIT[:12]}): {expected_sha256}\n"
            f"  Actual:                                   {actual}\n"
            "Reject dirty, mismatched, or uncommitted executor source."
        )

    spec = importlib.util.spec_from_file_location(
        "_ops10_executor_verified", executor_source_path
    )
    if spec is None or spec.loader is None:
        raise GeneratorError(
            f"Cannot build import spec from {executor_source_path}"
        )

    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclasses can resolve annotation strings
    # when the module processes frozen dataclasses under `from __future__ import annotations`.
    _mod_key = "_ops10_executor_verified"
    sys.modules[_mod_key] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        sys.modules.pop(_mod_key, None)
        raise GeneratorError(
            f"Failed to exec executor source {executor_source_path}: {exc}"
        ) from exc

    if not callable(getattr(module, "_contact_state_hash", None)):
        raise GeneratorError("Executor source does not export callable _contact_state_hash.")
    if not callable(getattr(module, "_load_group_manifest", None)):
        raise GeneratorError("Executor source does not export callable _load_group_manifest.")

    return module


# ── Snapshot loading ──────────────────────────────────────────────────────────


def _load_snapshot(
    path: Path,
    expected_count: int = EXPECTED_SNAPSHOT_COUNT,
) -> dict[str, dict[str, Any]]:
    """Load and validate snapshot JSON.  Returns {contact_id: contact_dict}."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GeneratorError(f"Cannot read snapshot: {exc}") from exc

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GeneratorError(f"Snapshot is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise GeneratorError("Snapshot root must be a JSON object.")

    if not data.get("exactCoreParity"):
        raise GeneratorError("Snapshot exactCoreParity is not true.")
    if not data.get("exactItemPayloadParity"):
        raise GeneratorError("Snapshot exactItemPayloadParity is not true.")

    contacts_raw = data.get("items")
    if not isinstance(contacts_raw, list):
        raise GeneratorError("Snapshot must contain an 'items' list.")

    if len(contacts_raw) != expected_count:
        raise GeneratorError(
            f"Snapshot has {len(contacts_raw)} contacts; expected {expected_count}."
        )

    contacts: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(contacts_raw):
        if not isinstance(c, dict):
            raise GeneratorError(f"Snapshot contact {i} is not a dict.")
        cid = str(c.get("id") or "").strip()
        if not cid:
            raise GeneratorError(f"Snapshot contact {i} has no id.")
        if cid in contacts:
            raise GeneratorError(f"Snapshot has duplicate contact ID: {cid!r}")
        contacts[cid] = c

    return contacts


# ── Reconciliation loading ────────────────────────────────────────────────────


def _load_reconciliation(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load and validate reconciliation JSON (flat list format).

    The root must be a JSON list of record objects.  Each record has 'id',
    'normalizedName', and 'recommendation' (KEEP / DELETE_CANDIDATE / HOLD).
    Records do not contain a group_id field — groups are formed by nonblank
    normalizedName; group_id is derived deterministically via _derive_group_id.
    A dictionary root or any synthetic envelope wrapper is rejected.

    Returns (groups, hold_ids).
    Each group has: group_id, name (== normalizedName), survivor_id, candidate_ids.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GeneratorError(f"Cannot read reconciliation: {exc}") from exc

    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GeneratorError(f"Reconciliation is not valid JSON: {exc}") from exc

    if isinstance(data, dict):
        raise GeneratorError(
            "Reconciliation root must be a JSON list of record objects, not a dict. "
            "Synthetic 'groups', 'records', or 'items' envelope wrappers are rejected."
        )
    if not isinstance(data, list):
        raise GeneratorError("Reconciliation root must be a JSON list.")

    # norm_name -> {"keep_id": str|None, "candidates": [str, ...]}
    groups_by_norm: dict[str, dict[str, Any]] = {}
    hold_ids: list[str] = []
    seen_ids: set[str] = set()

    for i, record in enumerate(data):
        if not isinstance(record, dict):
            raise GeneratorError(f"Record {i} is not a JSON object.")

        rec_id = str(record.get("id") or "").strip()
        if not rec_id:
            raise GeneratorError(f"Record {i} has no id.")
        if rec_id in seen_ids:
            raise GeneratorError(f"Duplicate contact ID in reconciliation: {rec_id!r}")
        seen_ids.add(rec_id)

        rec = record.get("recommendation", "")

        if rec == "HOLD":
            hold_ids.append(rec_id)
            continue

        if rec not in ("KEEP", "DELETE_CANDIDATE"):
            raise GeneratorError(
                f"Record {i} ({rec_id!r}) has unexpected recommendation: {rec!r}; "
                "expected KEEP, DELETE_CANDIDATE, or HOLD."
            )

        norm_name = str(record.get("normalizedName") or "").strip()
        if not norm_name:
            raise GeneratorError(
                f"Record {i} ({rec_id!r}) has a blank or missing normalizedName."
            )

        if norm_name not in groups_by_norm:
            groups_by_norm[norm_name] = {"keep_id": None, "candidates": []}

        if rec == "KEEP":
            if groups_by_norm[norm_name]["keep_id"] is not None:
                raise GeneratorError(
                    f"Multiple KEEP records for normalizedName {norm_name!r}."
                )
            groups_by_norm[norm_name]["keep_id"] = rec_id
        else:
            groups_by_norm[norm_name]["candidates"].append(rec_id)

    # Validate each group and build result; detect derived group_id collisions.
    seen_group_ids: set[str] = set()
    groups: list[dict[str, Any]] = []

    for norm_name, g in groups_by_norm.items():
        keep_id = g["keep_id"]
        candidates = g["candidates"]

        if keep_id is None:
            raise GeneratorError(
                f"DELETE_CANDIDATE record(s) for normalizedName {norm_name!r} "
                "have no matching KEEP record."
            )
        if not candidates:
            raise GeneratorError(
                f"KEEP record for normalizedName {norm_name!r} has no DELETE_CANDIDATE records."
            )

        group_id = _derive_group_id(norm_name)
        if group_id in seen_group_ids:
            raise GeneratorError(
                f"Derived group_id collision for normalizedName {norm_name!r}: {group_id!r}"
            )
        seen_group_ids.add(group_id)

        groups.append(
            {
                "group_id": group_id,
                "name": norm_name,
                "survivor_id": keep_id,
                "candidate_ids": candidates,
            }
        )

    return groups, hold_ids


# ── Count validation ──────────────────────────────────────────────────────────


def _validate_counts(
    groups: list[dict[str, Any]],
    hold_ids: list[str],
    snapshot_contacts: dict[str, dict[str, Any]],
    expected_keep: int = EXPECTED_KEEP_GROUPS,
    expected_delete: int = EXPECTED_DELETE_CANDIDATES,
    expected_hold: int = EXPECTED_HOLD_RECORDS,
    expected_in_scope: int = EXPECTED_IN_SCOPE_IDS,
) -> None:
    """Validate exact counts and ID coverage. Raises GeneratorError on any mismatch."""
    keep_count = len(groups)
    if keep_count != expected_keep:
        raise GeneratorError(
            f"Expected {expected_keep} KEEP groups; got {keep_count}."
        )

    delete_count = sum(len(g["candidate_ids"]) for g in groups)
    if delete_count != expected_delete:
        raise GeneratorError(
            f"Expected {expected_delete} DELETE_CANDIDATE records; got {delete_count}."
        )

    hold_count = len(hold_ids)
    if hold_count != expected_hold:
        raise GeneratorError(
            f"Expected {expected_hold} HOLD records; got {hold_count}."
        )

    in_scope_ids: set[str] = set()
    for g in groups:
        in_scope_ids.add(g["survivor_id"])
        for cid in g["candidate_ids"]:
            in_scope_ids.add(cid)

    if len(in_scope_ids) != expected_in_scope:
        raise GeneratorError(
            f"Expected {expected_in_scope} unique in-scope IDs; "
            f"got {len(in_scope_ids)} (duplicate IDs across groups?)."
        )

    for iid in in_scope_ids:
        if iid not in snapshot_contacts:
            raise GeneratorError(
                f"In-scope ID {iid!r} not found in snapshot."
            )

    hold_set = set(hold_ids)
    overlap = in_scope_ids & hold_set
    if overlap:
        raise GeneratorError(
            f"IDs appear in both in-scope and HOLD sets: {sorted(overlap)}"
        )


# ── Group-ID derivation ───────────────────────────────────────────────────────


def _derive_group_id(normalized_name: str) -> str:
    """ops10- + first 24 lowercase hex chars of SHA-256(UTF-8 normalizedName)."""
    digest = _sha256_hex(normalized_name.encode("utf-8"))
    return f"ops10-{digest[:24]}"


# ── Candidate ordering ────────────────────────────────────────────────────────


def _sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort candidates by lastModifiedDateTime (ascending), then id."""

    def _key(c: dict[str, Any]) -> tuple[str, str]:
        return (str(c.get("lastModifiedDateTime") or ""), str(c.get("id") or ""))

    return sorted(candidates, key=_key)


# ── Manifest identity ─────────────────────────────────────────────────────────


def _make_manifest_id(snapshot_path: Path, reconciliation_path: Path) -> str:
    """Deterministic manifest_id from input hashes, decision revision, and generator version."""
    snap_hash = _sha256_hex(snapshot_path.read_bytes())
    recon_hash = _sha256_hex(reconciliation_path.read_bytes())
    combined = (
        f"{snap_hash}:{recon_hash}:{DECISION_REVISION}:{GENERATOR_VERSION}"
    ).encode("utf-8")
    digest = _sha256_hex(combined)[:16]
    return f"ops10-{DECISION_REVISION}-{digest}"


def _make_trigger(manifest_id: str, group_id: str) -> str:
    return f"OPS-10 manifest {manifest_id} group {group_id}"


# ── Group manifest construction ───────────────────────────────────────────────


def _build_group_manifest(
    manifest_id: str,
    target_user: str,
    groups: list[dict[str, Any]],
    snapshot_contacts: dict[str, dict[str, Any]],
    contact_state_hash: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Build the executor-ready group manifest dict.

    Returns (manifest_dict, redacted_group_summaries, total_note_decisions).
    """
    group_entries: list[dict[str, Any]] = []
    redacted_groups: list[dict[str, Any]] = []
    total_note_decisions = 0

    for g in groups:
        group_id = g["group_id"]
        group_name = g["name"]
        survivor_id = g["survivor_id"]

        survivor = snapshot_contacts[survivor_id]
        raw_candidates = [snapshot_contacts[cid] for cid in g["candidate_ids"]]
        candidates = _sort_candidates(raw_candidates)

        merged_payload, note_decisions = _merge_contact(group_name, survivor, candidates)
        total_note_decisions += note_decisions

        all_ids = [survivor_id] + [c["id"] for c in candidates]
        approved_before_hashes: dict[str, str] = {}
        approved_etags: dict[str, str] = {}
        for cid in all_ids:
            contact = snapshot_contacts[cid]
            approved_before_hashes[cid] = contact_state_hash(contact)
            approved_etags[cid] = str(contact.get("@odata.etag") or "")

        candidate_delete_order = [c["id"] for c in candidates]
        trigger = _make_trigger(manifest_id, group_id)

        group_entry: dict[str, Any] = {
            "group_id": group_id,
            "survivor_id": survivor_id,
            "candidate_ids": [c["id"] for c in candidates],
            "approved_before_hashes": approved_before_hashes,
            "approved_etags": approved_etags,
            "desired_survivor_payload": merged_payload,
            "managed_fields": list(MUTABLE_FIELDS),
            "candidate_delete_order": candidate_delete_order,
            "trigger": trigger,
        }
        group_entries.append(group_entry)

        # Redacted summary: field names only, no contact data values.
        changed_fields: list[str] = []
        mv_additions: dict[str, int] = {}
        for field in MUTABLE_FIELDS:
            sv_val = survivor.get(field)
            merged_val = merged_payload[field]
            if sv_val != merged_val:
                changed_fields.append(field)
                if field in MULTI_VALUE_FIELDS:
                    sv_count = len(sv_val) if isinstance(sv_val, list) else 0
                    merged_count = len(merged_val) if isinstance(merged_val, list) else 0
                    delta = merged_count - sv_count
                    if delta > 0:
                        mv_additions[field] = delta

        decisions: list[str] = []
        if group_name in EXPLICIT_DECISION_NAMES:
            decisions.append(f"explicit_decision: {group_name}")

        redacted_groups.append(
            {
                "name": group_name,
                "company": str(survivor.get("companyName") or ""),
                "changed_fields": changed_fields,
                "multi_value_additions": mv_additions,
                "note_merge_count": note_decisions,
                "decisions": decisions,
                "candidate_count": len(candidates),
                "explicit_decision": group_name if group_name in EXPLICIT_DECISION_NAMES else None,
            }
        )

    manifest: dict[str, Any] = {
        "manifest_id": manifest_id,
        "target_user": target_user,
        "groups": group_entries,
    }
    return manifest, redacted_groups, total_note_decisions


# ── Pre-write manifest structure validation ───────────────────────────────────


def _validate_manifest_structure(
    manifest: dict[str, Any],
    groups_meta: list[dict[str, Any]],
    expected_keep: int = EXPECTED_KEEP_GROUPS,
    expected_delete: int = EXPECTED_DELETE_CANDIDATES,
    expected_in_scope: int = EXPECTED_IN_SCOPE_IDS,
) -> None:
    """Validate manifest structure before writing. Raises GeneratorError on any violation."""
    if "manifest_id" not in manifest:
        raise GeneratorError("Manifest is missing manifest_id.")
    if "target_user" not in manifest:
        raise GeneratorError("Manifest is missing target_user.")
    if "groups" not in manifest:
        raise GeneratorError("Manifest is missing groups.")

    groups = manifest["groups"]
    seen_contact_ids: set[str] = set()

    for g in groups:
        gid = g.get("group_id", "?")

        mf = list(g.get("managed_fields", []))
        mf_set = set(mf)

        # managed_fields must equal MUTABLE_FIELDS exactly.
        if mf_set != MUTABLE_FIELDS_SET:
            missing = MUTABLE_FIELDS_SET - mf_set
            extra = mf_set - MUTABLE_FIELDS_SET
            raise GeneratorError(
                f"Group {gid}: managed_fields mismatch. "
                f"Missing: {sorted(missing)}, Extra: {sorted(extra)}"
            )

        # No forbidden fields in managed_fields.
        bad_mf = mf_set & _IMMUTABLE_FIELDS
        if bad_mf:
            raise GeneratorError(
                f"Group {gid}: immutable field(s) in managed_fields: {sorted(bad_mf)}"
            )

        payload = g.get("desired_survivor_payload", {})
        payload_keys = set(payload.keys())

        # Payload keys must equal managed_fields exactly.
        if payload_keys != mf_set:
            raise GeneratorError(
                f"Group {gid}: payload keys do not match managed_fields."
            )

        # No forbidden fields in payload.
        bad_pl = payload_keys & _IMMUTABLE_FIELDS
        if bad_pl:
            raise GeneratorError(
                f"Group {gid}: immutable field(s) in payload: {sorted(bad_pl)}"
            )

        # approved_etags and approved_before_hashes keys must equal survivor + candidates.
        all_ids_in_group = set([g["survivor_id"]] + list(g["candidate_ids"]))
        etag_keys = set(g.get("approved_etags", {}).keys())
        hash_keys = set(g.get("approved_before_hashes", {}).keys())
        if etag_keys != all_ids_in_group:
            raise GeneratorError(
                f"Group {gid}: approved_etags keys mismatch."
            )
        if hash_keys != all_ids_in_group:
            raise GeneratorError(
                f"Group {gid}: approved_before_hashes keys mismatch."
            )

        # candidate_delete_order must be a permutation of candidate_ids.
        if set(g.get("candidate_delete_order", [])) != set(g.get("candidate_ids", [])):
            raise GeneratorError(
                f"Group {gid}: candidate_delete_order does not match candidate_ids."
            )

        for cid in all_ids_in_group:
            if cid in seen_contact_ids:
                raise GeneratorError(
                    f"Contact ID {cid!r} appears in more than one group."
                )
            seen_contact_ids.add(cid)

    total_candidates = sum(len(g.get("candidate_ids", [])) for g in groups)
    if len(groups) != expected_keep:
        raise GeneratorError(
            f"Expected {expected_keep} groups; got {len(groups)}."
        )
    if total_candidates != expected_delete:
        raise GeneratorError(
            f"Expected {expected_delete} candidates; got {total_candidates}."
        )
    if len(seen_contact_ids) != expected_in_scope:
        raise GeneratorError(
            f"Expected {expected_in_scope} unique contact IDs; "
            f"got {len(seen_contact_ids)}."
        )


# ── Executor-loader compatibility validation ──────────────────────────────────


def _validate_via_executor_loader(
    manifest_path: Path,
    manifest_sha256: str,
    target_user: str,
    executor_module: ModuleType,
    groups_meta: list[dict[str, Any]],
) -> None:
    """Load the manifest through the executor's _load_group_manifest and verify counts.

    Raises GeneratorError on any failure.
    """
    load_fn = executor_module._load_group_manifest  # type: ignore[attr-defined]

    try:
        loaded_id, loaded_groups = load_fn(manifest_path, manifest_sha256, target_user)
    except Exception as exc:
        raise GeneratorError(
            f"Executor _load_group_manifest rejected the manifest: {exc}"
        ) from exc

    if len(loaded_groups) != EXPECTED_KEEP_GROUPS:
        raise GeneratorError(
            f"Executor loaded {len(loaded_groups)} groups; expected {EXPECTED_KEEP_GROUPS}."
        )

    total_loaded_candidates = sum(len(g.candidate_ids) for g in loaded_groups)
    if total_loaded_candidates != EXPECTED_DELETE_CANDIDATES:
        raise GeneratorError(
            f"Executor loaded {total_loaded_candidates} candidates; "
            f"expected {EXPECTED_DELETE_CANDIDATES}."
        )

    # Validate two-candidate groups; keys are exact normalizedName values (lowercase).
    name_to_group_id: dict[str, str] = {g["name"]: g["group_id"] for g in groups_meta}
    gid_to_loaded = {g.group_id: g for g in loaded_groups}

    for two_cand_name in _TWO_CANDIDATE_NAMES:
        gid = name_to_group_id.get(two_cand_name)
        if gid is not None:
            lg = gid_to_loaded.get(gid)
            if lg is None:
                raise GeneratorError(
                    f"{two_cand_name} group {gid!r} not found in executor-loaded manifest."
                )
            if len(lg.candidate_ids) != 2:
                raise GeneratorError(
                    f"{two_cand_name} must have exactly 2 candidates; "
                    f"got {len(lg.candidate_ids)}."
                )


# ── Atomic write ──────────────────────────────────────────────────────────────


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Atomically write content to path with the given mode."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".ops10_tmp_")
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.rename(path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ── Redacted review ───────────────────────────────────────────────────────────


def _build_redacted_review(
    manifest_id: str,
    generator_version: str,
    decision_revision: str,
    group_count: int,
    candidate_count: int,
    in_scope_count: int,
    hold_count: int,
    note_decisions_before: int,
    note_decisions_after: int,
    redacted_groups: list[dict[str, Any]],
    approvable: bool,
) -> dict[str, Any]:
    """Build the redacted review object.

    Contains: names, company, changed field names, multi-value addition counts,
    note merge counts, decisions.  No IDs, ETags, hashes tied to individual
    contacts, phones, emails, addresses, note bodies, tokens, or secrets.
    """
    safe_groups = [
        {
            "name": g["name"],
            "company": g["company"],
            "changed_fields": g["changed_fields"],
            "multi_value_additions": g["multi_value_additions"],
            "note_merge_count": g["note_merge_count"],
            "decisions": g["decisions"],
            "candidate_count": g["candidate_count"],
            "explicit_decision": g["explicit_decision"],
        }
        for g in redacted_groups
    ]

    return {
        "approvable": approvable,
        "manifest_id": manifest_id,
        "generator_version": generator_version,
        "decision_revision": decision_revision,
        "summary": {
            "group_count": group_count,
            "candidate_count": candidate_count,
            "in_scope_id_count": in_scope_count,
            "excluded_hold_count": hold_count,
            "note_decisions_before_rule": note_decisions_before,
            "note_decisions_after_rule": note_decisions_after,
        },
        "groups": safe_groups,
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OPS-10 Merged-State Manifest Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--snapshot", required=True, help="Path to exact-parity OPS-10 snapshot JSON.")
    p.add_argument("--reconciliation", required=True, help="Path to item reconciliation JSON.")
    p.add_argument("--manifest-output", required=True, help="Path for the generated manifest JSON.")
    p.add_argument(
        "--redacted-review-output", required=True, help="Path for the redacted review JSON."
    )
    p.add_argument("--target-user", required=True, help="Target user UPN for the manifest.")
    p.add_argument(
        "--executor-source", required=True,
        help="Path to the OPS-17 executor source (SHA-256 verified).",
    )
    p.add_argument(
        "--executor-commit", required=True,
        help=f"Expected executor Git commit SHA (must be {EXECUTOR_COMMIT}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # ── 1. Validate executor commit argument. ─────────────────────────────────
    if args.executor_commit.strip() != EXECUTOR_COMMIT:
        print(
            f"ERROR: --executor-commit does not match expected commit.\n"
            f"  Expected: {EXECUTOR_COMMIT}\n"
            f"  Got:      {args.executor_commit.strip()}",
            file=sys.stderr,
        )
        return 1

    # ── 2. Verify and load executor source. ──────────────────────────────────
    try:
        executor_module = _verify_executor_source(Path(args.executor_source))
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    contact_state_hash = executor_module._contact_state_hash  # type: ignore[attr-defined]

    # ── 3. Load inputs. ───────────────────────────────────────────────────────
    snapshot_path = Path(args.snapshot)
    reconciliation_path = Path(args.reconciliation)

    try:
        snapshot_contacts = _load_snapshot(snapshot_path)
        groups, hold_ids = _load_reconciliation(reconciliation_path)
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ── 4. Validate counts. ───────────────────────────────────────────────────
    try:
        _validate_counts(groups, hold_ids, snapshot_contacts)
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ── 5. Build manifest. ────────────────────────────────────────────────────
    manifest_id = _make_manifest_id(snapshot_path, reconciliation_path)

    try:
        manifest, redacted_groups, total_note_decisions = _build_group_manifest(
            manifest_id, args.target_user, groups, snapshot_contacts, contact_state_hash
        )
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ── 6. Validate note decision counts. ────────────────────────────────────
    if total_note_decisions != EXPECTED_NOTE_DECISIONS_BEFORE:
        print(
            f"ERROR: Expected {EXPECTED_NOTE_DECISIONS_BEFORE} note decisions before "
            f"applying Clay's note rule; got {total_note_decisions}.",
            file=sys.stderr,
        )
        return 1
    # After rule is applied, all note decisions are resolved (count = 0).
    note_decisions_after = EXPECTED_NOTE_DECISIONS_AFTER

    # ── 7. Pre-write structural validation. ───────────────────────────────────
    try:
        _validate_manifest_structure(manifest, groups)
    except GeneratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # ── 8. Serialize manifest. ────────────────────────────────────────────────
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    manifest_sha256 = _sha256_hex(manifest_bytes)

    # ── 9. Write manifest atomically (mode 0600). ─────────────────────────────
    manifest_output = Path(args.manifest_output)
    try:
        _atomic_write(manifest_output, manifest_bytes, mode=0o600)
    except OSError as exc:
        print(f"ERROR writing manifest: {exc}", file=sys.stderr)
        return 1

    # ── 10. Validate via executor _load_group_manifest. ───────────────────────
    try:
        _validate_via_executor_loader(
            manifest_output, manifest_sha256, args.target_user, executor_module, groups
        )
    except GeneratorError as exc:
        print(f"ERROR: Executor validation: {exc}", file=sys.stderr)
        return 1

    # ── 11. Build redacted review. ────────────────────────────────────────────
    redacted_review = _build_redacted_review(
        manifest_id=manifest_id,
        generator_version=GENERATOR_VERSION,
        decision_revision=DECISION_REVISION,
        group_count=len(groups),
        candidate_count=sum(len(g["candidate_ids"]) for g in groups),
        in_scope_count=EXPECTED_IN_SCOPE_IDS,
        hold_count=len(hold_ids),
        note_decisions_before=total_note_decisions,
        note_decisions_after=note_decisions_after,
        redacted_groups=redacted_groups,
        approvable=True,
    )

    if not redacted_review["approvable"]:
        print("ERROR: Redacted review is not approvable.", file=sys.stderr)
        return 1

    # ── 12. Write redacted review atomically (mode 0600). ────────────────────
    redacted_output = Path(args.redacted_review_output)
    redacted_bytes = json.dumps(redacted_review, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        _atomic_write(redacted_output, redacted_bytes, mode=0o600)
    except OSError as exc:
        print(f"ERROR writing redacted review: {exc}", file=sys.stderr)
        return 1

    # ── 13. Print summary (no manifest content). ──────────────────────────────
    manifest_final_sha256 = _sha256_hex(manifest_output.read_bytes())
    redacted_sha256 = _sha256_hex(redacted_bytes)

    print(f"manifest-output:        {manifest_output}")
    print(f"manifest-sha256:        {manifest_final_sha256}")
    print(f"redacted-review-output: {redacted_output}")
    print(f"redacted-review-sha256: {redacted_sha256}")
    print(f"groups:                 {len(groups)}")
    print(f"candidates:             {sum(len(g['candidate_ids']) for g in groups)}")
    print(f"in-scope-ids:           {EXPECTED_IN_SCOPE_IDS}")
    print(f"hold-records:           {len(hold_ids)}")
    print(f"note-decisions-before:  {total_note_decisions}")
    print(f"note-decisions-after:   {note_decisions_after}")
    print(f"approvable:             {redacted_review['approvable']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
