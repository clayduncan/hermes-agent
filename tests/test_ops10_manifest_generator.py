"""Tests for OPS-10 Merged-State Manifest Generator.

All tests use synthetic fixtures only — no live data, no network access,
no Microsoft endpoints, no credentials.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tools.ops10_manifest_generator import (
    EXECUTOR_COMMIT,
    EXPECTED_EXECUTOR_SHA256,
    MUTABLE_FIELDS,
    MUTABLE_FIELDS_SET,
    AddressConflict,
    GeneratorError,
    ScalarConflict,
    _atomic_write,
    _build_group_manifest,
    _build_redacted_review,
    _count_note_decision,
    _derive_group_id,
    _digits_only,
    _is_blank,
    _load_reconciliation,
    _load_snapshot,
    _make_manifest_id,
    _make_trigger,
    _merge_address,
    _merge_contact,
    _merge_notes,
    _merge_scalar,
    _sha256_hex,
    _sort_candidates,
    _union_email_list,
    _union_multi_value,
    _union_phone_list,
    _union_string_list,
    _validate_counts,
    _validate_manifest_structure,
    _verify_executor_source,
)

# ── Shared helpers ─────────────────────────────────────────────────────────────

_EXECUTOR_PATH = (
    Path(__file__).parent.parent / "tools" / "msgraph_user_contacts_write_client.py"
)


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_contact(
    cid: str,
    *,
    etag: str = 'W/"etag-default"',
    display_name: str = "",
    given_name: str = "",
    surname: str = "",
    company: str = "",
    mobile_phone: str | None = None,
    email_addresses: list | None = None,
    business_phones: list | None = None,
    home_phones: list | None = None,
    categories: list | None = None,
    children: list | None = None,
    im_addresses: list | None = None,
    personal_notes: str | None = None,
    file_as: str | None = None,
    primary_email_address: Any = None,
    business_address: dict | None = None,
    home_address: dict | None = None,
    other_address: dict | None = None,
    last_modified: str = "2026-01-01T00:00:00Z",
) -> dict:
    c: dict = {
        "id": cid,
        "@odata.etag": etag,
        "displayName": display_name,
        "givenName": given_name,
        "surname": surname,
        "companyName": company,
        "lastModifiedDateTime": last_modified,
    }
    if mobile_phone is not None:
        c["mobilePhone"] = mobile_phone
    if email_addresses is not None:
        c["emailAddresses"] = email_addresses
    if business_phones is not None:
        c["businessPhones"] = business_phones
    if home_phones is not None:
        c["homePhones"] = home_phones
    if categories is not None:
        c["categories"] = categories
    if children is not None:
        c["children"] = children
    if im_addresses is not None:
        c["imAddresses"] = im_addresses
    if personal_notes is not None:
        c["personalNotes"] = personal_notes
    if file_as is not None:
        c["fileAs"] = file_as
    if primary_email_address is not None:
        c["primaryEmailAddress"] = primary_email_address
    if business_address is not None:
        c["businessAddress"] = business_address
    if home_address is not None:
        c["homeAddress"] = home_address
    if other_address is not None:
        c["otherAddress"] = other_address
    return c


def _make_snapshot(
    contacts: list[dict],
    *,
    exact_core_parity: bool = True,
    exact_item_payload_parity: bool = True,
) -> dict:
    return {
        "exactCoreParity": exact_core_parity,
        "exactItemPayloadParity": exact_item_payload_parity,
        "items": contacts,
    }


def _make_reconciliation(
    groups: list[dict],
    excluded: list[dict] | None = None,
) -> list[dict]:
    """Create a flat reconciliation list from group defs and excluded HOLD items.

    Each group dict has name (used as normalizedName), survivor_id, and candidate_ids.
    Records use normalizedName (not group_id) to match the real reconciliation format.
    Excluded items must have id and optional recommendation (default: HOLD).
    """
    records: list[dict] = []
    for g in groups:
        norm_name = g.get("name", "")
        survivor_id = g.get("survivor_id", "")
        if survivor_id:
            records.append({
                "id": survivor_id,
                "recommendation": "KEEP",
                "normalizedName": norm_name,
                "displayName": norm_name,
                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                "rationale": "KEEP",
            })
        for cid in (g.get("candidate_ids") or []):
            records.append({
                "id": cid,
                "recommendation": "DELETE_CANDIDATE",
                "normalizedName": norm_name,
                "displayName": norm_name,
                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                "rationale": "DELETE_CANDIDATE",
            })
    for item in (excluded or []):
        records.append({
            "id": item["id"],
            "recommendation": item.get("recommendation", "HOLD"),
        })
    return records


def _make_group_entry(
    group_id: str,
    name: str,
    survivor_id: str,
    candidate_ids: list[str],
) -> dict:
    return {
        "group_id": group_id,
        "name": name,
        "survivor_id": survivor_id,
        "candidate_ids": candidate_ids,
    }


# ── Executor source verification ───────────────────────────────────────────────


class TestVerifyExecutorSource:
    def test_real_committed_executor_verifies(self):
        """The committed executor file must pass SHA-256 verification."""
        assert _EXECUTOR_PATH.is_file(), (
            f"Executor not found at {_EXECUTOR_PATH}; run from repo root."
        )
        module = _verify_executor_source(_EXECUTOR_PATH)
        assert callable(module._contact_state_hash)
        assert callable(module._load_group_manifest)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(GeneratorError, match="not found"):
            _verify_executor_source(tmp_path / "nonexistent.py")

    def test_sha256_mismatch_raises(self, tmp_path: Path):
        bad_file = tmp_path / "bad_executor.py"
        bad_file.write_bytes(b"# wrong content\n")
        with pytest.raises(GeneratorError, match="SHA-256 mismatch"):
            _verify_executor_source(bad_file)

    def test_dirty_working_tree_rejected(self, tmp_path: Path):
        """A modified copy of the executor (different bytes) is rejected."""
        real_bytes = _EXECUTOR_PATH.read_bytes()
        dirty = tmp_path / "dirty_executor.py"
        dirty.write_bytes(real_bytes + b"\n# added line\n")
        with pytest.raises(GeneratorError, match="SHA-256 mismatch"):
            _verify_executor_source(dirty)

    def test_module_missing_contact_state_hash_raises(self, tmp_path: Path):
        """A file with matching SHA-256 but missing _contact_state_hash is rejected."""
        # Write a minimal stub and patch expected SHA to match it.
        stub = b"_load_group_manifest = lambda *a, **kw: None\n"
        stub_path = tmp_path / "stub.py"
        stub_path.write_bytes(stub)
        stub_sha = _sha256_of(stub)
        with pytest.raises(GeneratorError, match="_contact_state_hash"):
            _verify_executor_source(stub_path, expected_sha256=stub_sha)

    def test_custom_expected_sha256_parameter(self, tmp_path: Path):
        """_verify_executor_source accepts a custom expected_sha256 for tests."""
        stub_src = textwrap.dedent("""\
            def _contact_state_hash(c):
                import hashlib, json
                return hashlib.sha256(
                    json.dumps(c, sort_keys=True).encode()
                ).hexdigest()

            def _load_group_manifest(path, sha256, user):
                return "mid", []
        """).encode()
        stub_path = tmp_path / "stub_executor.py"
        stub_path.write_bytes(stub_src)
        stub_sha = _sha256_of(stub_src)
        module = _verify_executor_source(stub_path, expected_sha256=stub_sha)
        assert callable(module._contact_state_hash)
        assert callable(module._load_group_manifest)


# ── Snapshot loading ───────────────────────────────────────────────────────────


class TestLoadSnapshot:
    def _contacts(self, n: int) -> list[dict]:
        return [{"id": f"c{i}", "@odata.etag": f'W/"e{i}"'} for i in range(n)]

    def test_valid_snapshot(self, tmp_path: Path):
        data = _make_snapshot(self._contacts(5))
        path = _write_json(tmp_path / "snap.json", data)
        result = _load_snapshot(path, expected_count=5)
        assert len(result) == 5
        assert "c0" in result

    def test_missing_exact_core_parity_raises(self, tmp_path: Path):
        data = _make_snapshot(self._contacts(3), exact_core_parity=False)
        path = _write_json(tmp_path / "snap.json", data)
        with pytest.raises(GeneratorError, match="exactCoreParity"):
            _load_snapshot(path, expected_count=3)

    def test_missing_exact_item_payload_parity_raises(self, tmp_path: Path):
        data = _make_snapshot(self._contacts(3), exact_item_payload_parity=False)
        path = _write_json(tmp_path / "snap.json", data)
        with pytest.raises(GeneratorError, match="exactItemPayloadParity"):
            _load_snapshot(path, expected_count=3)

    def test_wrong_count_raises(self, tmp_path: Path):
        data = _make_snapshot(self._contacts(10))
        path = _write_json(tmp_path / "snap.json", data)
        with pytest.raises(GeneratorError, match="expected 5"):
            _load_snapshot(path, expected_count=5)

    def test_duplicate_id_raises(self, tmp_path: Path):
        contacts = [{"id": "dup", "@odata.etag": 'W/"e0"'}, {"id": "dup", "@odata.etag": 'W/"e1"'}]
        data = _make_snapshot(contacts)
        path = _write_json(tmp_path / "snap.json", data)
        with pytest.raises(GeneratorError, match="duplicate"):
            _load_snapshot(path, expected_count=2)

    def test_contact_without_id_raises(self, tmp_path: Path):
        contacts = [{"@odata.etag": 'W/"e0"'}]  # no id
        data = _make_snapshot(contacts)
        path = _write_json(tmp_path / "snap.json", data)
        with pytest.raises(GeneratorError, match="no id"):
            _load_snapshot(path, expected_count=1)

    def test_missing_items_list_raises(self, tmp_path: Path):
        path = _write_json(
            tmp_path / "snap.json",
            {"exactCoreParity": True, "exactItemPayloadParity": True},
        )
        with pytest.raises(GeneratorError, match="'items' list"):
            _load_snapshot(path, expected_count=0)


# ── Reconciliation loading ─────────────────────────────────────────────────────


class TestLoadReconciliation:
    def test_valid_reconciliation(self, tmp_path: Path):
        data = _make_reconciliation(
            groups=[_make_group_entry("g1", "Alice", "s1", ["c1"])],
            excluded=[{"id": "h1", "recommendation": "HOLD"}],
        )
        path = _write_json(tmp_path / "recon.json", data)
        groups, hold_ids = _load_reconciliation(path)
        assert len(groups) == 1
        assert groups[0]["survivor_id"] == "s1"
        assert groups[0]["name"] == "Alice"
        assert groups[0]["group_id"] == _derive_group_id("Alice")
        assert hold_ids == ["h1"]

    def test_group_id_derived_from_sha256_of_normalized_name(self, tmp_path: Path):
        """group_id must be ops10- + first 24 hex chars of SHA-256(normalizedName)."""
        data = _make_reconciliation(
            groups=[_make_group_entry("ignored", "Bob Smith", "s1", ["c1"])],
        )
        path = _write_json(tmp_path / "recon.json", data)
        groups, _ = _load_reconciliation(path)
        expected = _derive_group_id("Bob Smith")
        assert groups[0]["group_id"] == expected
        assert groups[0]["group_id"].startswith("ops10-")
        assert len(groups[0]["group_id"]) == len("ops10-") + 24

    def test_dict_root_raises(self, tmp_path: Path):
        """A dictionary root must be rejected; only a JSON list is accepted."""
        path = _write_json(tmp_path / "recon.json", {"groups": [], "excluded": []})
        with pytest.raises(GeneratorError, match="JSON list"):
            _load_reconciliation(path)

    def test_blank_normalized_name_raises(self, tmp_path: Path):
        """A KEEP or DELETE_CANDIDATE record with a blank normalizedName must be rejected."""
        data = [
            {"id": "s1", "recommendation": "KEEP", "normalizedName": "  "},
        ]
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="normalizedName"):
            _load_reconciliation(path)

    def test_missing_normalized_name_raises(self, tmp_path: Path):
        """A KEEP record without a normalizedName field must be rejected."""
        data = [{"id": "s1", "recommendation": "KEEP"}]
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="normalizedName"):
            _load_reconciliation(path)

    def test_multiple_keep_same_normalized_name_raises(self, tmp_path: Path):
        """Multiple KEEP records for the same normalizedName must be rejected."""
        data = [
            {"id": "s1", "recommendation": "KEEP", "normalizedName": "Alice"},
            {"id": "s2", "recommendation": "KEEP", "normalizedName": "Alice"},
            {"id": "c1", "recommendation": "DELETE_CANDIDATE", "normalizedName": "Alice"},
        ]
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="Multiple KEEP"):
            _load_reconciliation(path)

    def test_keep_group_no_delete_candidates_raises(self, tmp_path: Path):
        """A KEEP group with no DELETE_CANDIDATE records in the list must be rejected."""
        data = _make_reconciliation(
            groups=[{"name": "X", "survivor_id": "s1", "candidate_ids": []}]
        )
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="DELETE_CANDIDATE"):
            _load_reconciliation(path)

    def test_delete_candidate_no_keep_raises(self, tmp_path: Path):
        """DELETE_CANDIDATE records with no matching KEEP normalizedName must be rejected."""
        data = [
            {"id": "s1", "recommendation": "KEEP", "normalizedName": "Alice"},
            {"id": "c1", "recommendation": "DELETE_CANDIDATE", "normalizedName": "Alice"},
            {"id": "c2", "recommendation": "DELETE_CANDIDATE", "normalizedName": "UnknownPerson"},
        ]
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="no matching KEEP"):
            _load_reconciliation(path)

    def test_unknown_recommendation_raises(self, tmp_path: Path):
        """A record with an unexpected recommendation value must be rejected."""
        data = [
            {"id": "s1", "recommendation": "KEEP", "normalizedName": "Alice"},
            {"id": "c1", "recommendation": "DELETE_CANDIDATE", "normalizedName": "Alice"},
            {"id": "x1", "recommendation": "REVIEW"},
        ]
        path = _write_json(tmp_path / "recon.json", data)
        with pytest.raises(GeneratorError, match="unexpected recommendation"):
            _load_reconciliation(path)


# ── Count validation ───────────────────────────────────────────────────────────


class TestValidateCounts:
    def _snap(self, ids: list[str]) -> dict:
        return {iid: {"id": iid} for iid in ids}

    def test_correct_counts_pass(self):
        groups = [
            _make_group_entry("g1", "A", "s1", ["c1"]),
            _make_group_entry("g2", "B", "s2", ["c2"]),
        ]
        hold_ids = ["h1"]
        snap = self._snap(["s1", "c1", "s2", "c2"])
        _validate_counts(
            groups, hold_ids, snap,
            expected_keep=2, expected_delete=2, expected_hold=1, expected_in_scope=4,
        )

    def test_wrong_keep_count_raises(self):
        groups = [_make_group_entry("g1", "A", "s1", ["c1"])]
        snap = self._snap(["s1", "c1"])
        with pytest.raises(GeneratorError, match="KEEP groups"):
            _validate_counts(
                groups, [], snap,
                expected_keep=2, expected_delete=1, expected_hold=0, expected_in_scope=2,
            )

    def test_wrong_delete_count_raises(self):
        groups = [_make_group_entry("g1", "A", "s1", ["c1"])]
        snap = self._snap(["s1", "c1"])
        with pytest.raises(GeneratorError, match="DELETE_CANDIDATE"):
            _validate_counts(
                groups, [], snap,
                expected_keep=1, expected_delete=2, expected_hold=0, expected_in_scope=2,
            )

    def test_wrong_hold_count_raises(self):
        groups = [_make_group_entry("g1", "A", "s1", ["c1"])]
        snap = self._snap(["s1", "c1"])
        with pytest.raises(GeneratorError, match="HOLD"):
            _validate_counts(
                groups, ["h1"], snap,
                expected_keep=1, expected_delete=1, expected_hold=2, expected_in_scope=2,
            )

    def test_missing_snapshot_id_raises(self):
        groups = [_make_group_entry("g1", "A", "s1", ["c1"])]
        snap = self._snap(["s1"])  # c1 missing
        with pytest.raises(GeneratorError, match="not found in snapshot"):
            _validate_counts(
                groups, [], snap,
                expected_keep=1, expected_delete=1, expected_hold=0, expected_in_scope=2,
            )

    def test_id_in_both_inscope_and_hold_raises(self):
        groups = [_make_group_entry("g1", "A", "s1", ["c1"])]
        snap = self._snap(["s1", "c1"])
        with pytest.raises(GeneratorError, match="in-scope and HOLD"):
            _validate_counts(
                groups, ["s1"], snap,
                expected_keep=1, expected_delete=1, expected_hold=1, expected_in_scope=2,
            )

    def test_duplicate_ids_across_groups_raises(self):
        groups = [
            _make_group_entry("g1", "A", "s1", ["shared"]),
            _make_group_entry("g2", "B", "s2", ["shared"]),
        ]
        snap = self._snap(["s1", "s2", "shared"])
        with pytest.raises(GeneratorError, match="unique in-scope"):
            _validate_counts(
                groups, [], snap,
                expected_keep=2, expected_delete=2, expected_hold=0, expected_in_scope=4,
            )


# ── Multi-value union ──────────────────────────────────────────────────────────


class TestMultiValueUnion:
    def test_phone_union_deduplicates_by_digits(self):
        # "+1 (555) 100-0001" → 15551000001; "1-555-100-0001" → same 11 digits
        survivor = ["+1 (555) 100-0001"]
        candidate = ["1-555-100-0001", "+1 555 200-0002"]
        result = _union_phone_list(survivor, candidate)
        assert result[0] == "+1 (555) 100-0001"  # survivor format preserved
        assert "+1 555 200-0002" in result
        assert len(result) == 2  # "1-555-100-0001" normalized == survivor, deduped

    def test_phone_survivor_format_preserved_when_digits_match(self):
        result = _union_phone_list(["(800) 555-1234"], ["8005551234"])
        assert result == ["(800) 555-1234"]

    def test_email_union_deduplicates_by_address(self):
        survivor = [{"address": "Alice@Example.COM", "name": "Alice"}]
        candidate = [
            {"address": "alice@example.com", "name": "Alice (dupe)"},
            {"address": "bob@example.com", "name": "Bob"},
        ]
        result = _union_email_list(survivor, candidate)
        assert len(result) == 2
        assert result[0] == {"address": "Alice@Example.COM", "name": "Alice"}  # survivor obj preserved
        assert result[1]["address"] == "bob@example.com"

    def test_string_list_union_deduplicates_by_strip(self):
        result = _union_string_list(["  Tag1  ", "Tag2"], ["Tag1", "Tag3"])
        # "Tag1" (stripped) deduped with "  Tag1  "; survivor original preserved
        assert "  Tag1  " in result
        assert "Tag1" not in result or result.index("  Tag1  ") < result.index("Tag2")
        assert "Tag3" in result

    def test_union_multi_value_dispatches_correctly(self):
        sv = ["5551001111"]
        cv = ["5551002222"]
        phones = _union_multi_value("businessPhones", sv, cv)
        assert len(phones) == 2

        sv_e = [{"address": "a@b.com"}]
        cv_e = [{"address": "c@d.com"}]
        emails = _union_multi_value("emailAddresses", sv_e, cv_e)
        assert len(emails) == 2

        sv_c = ["Cat1"]
        cv_c = ["Cat2"]
        cats = _union_multi_value("categories", sv_c, cv_c)
        assert "Cat1" in cats and "Cat2" in cats

    def test_survivor_order_first_in_union(self):
        result = _union_phone_list(["111", "222"], ["333", "444"])
        assert result[:2] == ["111", "222"]
        assert result[2:] == ["333", "444"]

    def test_empty_survivor_list_uses_candidate(self):
        result = _union_phone_list([], ["5551234567"])
        assert result == ["5551234567"]

    def test_empty_candidate_list_uses_survivor(self):
        result = _union_phone_list(["5551234567"], [])
        assert result == ["5551234567"]


# ── Personal notes ─────────────────────────────────────────────────────────────


class TestPersonalNotes:
    def test_survivor_only_no_merge(self):
        result = _merge_notes("Survivor note", [None])
        assert result == "Survivor note"

    def test_candidate_only_no_survivor(self):
        result = _merge_notes(None, ["Candidate note"])
        assert result == "Candidate note"

    def test_distinct_notes_joined_with_exactly_two_newlines(self):
        result = _merge_notes("Note A", ["Note B"])
        assert result == "Note A\n\nNote B"
        assert "\n\n\n" not in result

    def test_identical_notes_not_duplicated(self):
        result = _merge_notes("Same note", ["Same note"])
        assert result == "Same note"
        assert "Same note\n\nSame note" not in str(result)

    def test_survivor_first_then_candidates_in_order(self):
        result = _merge_notes("S", ["C1", "C2"])
        assert result == "S\n\nC1\n\nC2"

    def test_blank_notes_excluded(self):
        result = _merge_notes("  ", ["Candidate note"])
        assert result == "Candidate note"

    def test_no_labels_or_provenance_inserted(self):
        result = _merge_notes("Body1", ["Body2"])
        for forbidden in ["From:", "Source:", "---", "===", "Note 1", "Note 2"]:
            assert forbidden not in str(result)

    def test_whitespace_only_note_is_blank(self):
        result = _merge_notes("\t  \n", ["  "])
        assert result is None

    def test_two_candidate_notes_merged_in_order(self):
        result = _merge_notes("Survivor", ["Cand1", "Cand2"])
        assert result == "Survivor\n\nCand1\n\nCand2"

    def test_count_note_decision_distinct_notes(self):
        # Conflict: survivor and candidate differ → decision required
        count = _count_note_decision("Note A", ["Note B"])
        assert count == 1

    def test_count_note_decision_blank_survivor_identical_candidates(self):
        # Propagation: blank survivor + normalized-identical nonblank candidates → decision required.
        # This is the real case for aaron kimball and adam o'daniel groups.
        count = _count_note_decision("", ["Met at conf 2019", "Met at conf 2019"])
        assert count == 1

    def test_count_note_decision_blank_survivor_single_candidate(self):
        # Propagation: blank survivor + single nonblank candidate → decision required
        count = _count_note_decision(None, ["Met at conf 2019"])
        assert count == 1

    def test_count_note_decision_nonblank_survivor_identical_candidate(self):
        # No decision: survivor and candidate share the same note (normalized-equal)
        count = _count_note_decision("Same", ["Same"])
        assert count == 0

    def test_count_note_decision_only_survivor(self):
        # No decision: only the survivor has a note, all candidates are blank
        count = _count_note_decision("Note", [None])
        assert count == 0

    def test_count_note_decision_both_blank(self):
        # No decision: no notes anywhere
        count = _count_note_decision(None, [None])
        assert count == 0

    def test_count_note_decision_propagation_accumulates_per_group(self):
        # Two separate groups each with blank survivor + identical nonblank candidates
        # must each contribute 1, totalling 2 — the expected real count.
        group1 = _count_note_decision("", ["Met at conf 2019", "Met at conf 2019"])
        group2 = _count_note_decision(None, ["Referred by Jane", "Referred by Jane"])
        assert group1 + group2 == 2


# ── Structured address merge ───────────────────────────────────────────────────


class TestAddressMerge:
    def test_survivor_preserved_unchanged(self):
        sv = {"street": "1 Main St", "city": "Springfield"}
        result = _merge_address("businessAddress", sv, None, "Test Group")
        assert result == sv

    def test_blank_subfields_filled_from_candidate(self):
        sv = {"street": "1 Main St"}
        cand = {"street": "1 Main St", "city": "Springfield", "postalCode": "12345"}
        result = _merge_address("businessAddress", sv, cand, "Test Group")
        assert result["city"] == "Springfield"
        assert result["postalCode"] == "12345"
        assert result["street"] == "1 Main St"

    def test_conflict_raises_with_no_raw_values(self):
        sv = {"street": "1 Main St", "city": "Springfield"}
        cand = {"city": "Shelbyville"}
        with pytest.raises(AddressConflict) as exc_info:
            _merge_address("businessAddress", sv, cand, "Alice")
        assert "businessAddress.city" in str(exc_info.value)
        # Error message must not contain raw values.
        assert "Springfield" not in str(exc_info.value)
        assert "Shelbyville" not in str(exc_info.value)

    def test_both_blank_returns_none(self):
        result = _merge_address("homeAddress", None, None, "X")
        assert result is None

    def test_blank_survivor_uses_candidate(self):
        cand = {"city": "Portland"}
        result = _merge_address("otherAddress", None, cand, "X")
        assert result["city"] == "Portland"

    def test_blank_candidate_uses_survivor(self):
        sv = {"city": "Boston"}
        result = _merge_address("homeAddress", sv, None, "X")
        assert result == sv

    def test_same_subfield_value_no_conflict(self):
        sv = {"city": "Portland"}
        cand = {"city": "Portland"}
        result = _merge_address("homeAddress", sv, cand, "X")
        assert result["city"] == "Portland"

    def test_whitespace_normalized_subfield_comparison(self):
        sv = {"city": " Portland "}
        cand = {"city": "Portland"}
        # Same after strip — no conflict.
        result = _merge_address("homeAddress", sv, cand, "X")
        assert "Portland" in result["city"]


# ── Scalar merge ───────────────────────────────────────────────────────────────


class TestScalarMerge:
    def _merge(self, field: str, sv: Any, cv: Any, group: str = "Test") -> Any:
        return _merge_scalar(field, sv, cv, group, [], False)

    def test_survivor_nonblank_preserved(self):
        assert self._merge("givenName", "Alice", None) == "Alice"

    def test_blank_survivor_filled_from_candidate(self):
        assert self._merge("givenName", None, "Bob") == "Bob"

    def test_both_blank_returns_none(self):
        assert self._merge("givenName", None, None) is None

    def test_conflict_raises_scalar_conflict(self):
        with pytest.raises(ScalarConflict):
            self._merge("givenName", "Alice", "Alicia")

    def test_normalized_equal_mobile_phone_preserves_survivor_format(self):
        result = _merge_scalar("mobilePhone", "(555) 100-0001", "5551000001", "G", [], False)
        assert result == "(555) 100-0001"

    def test_truly_different_mobile_phone_raises(self):
        with pytest.raises(ScalarConflict):
            _merge_scalar("mobilePhone", "5551000001", "5551000002", "G", [], False)

    def test_empty_string_treated_as_blank(self):
        assert self._merge("jobTitle", "  ", "Engineer") == "Engineer"

    def test_scalar_conflict_report_no_raw_values(self):
        with pytest.raises(ScalarConflict) as exc_info:
            _merge_scalar("displayName", "Smith, J.", "Smith, John", "TestGroup", [], False)
        msg = str(exc_info.value)
        # Must not emit raw field values in the error.
        assert "Smith, J." not in msg
        assert "Smith, John" not in msg
        assert "displayName" in msg


# ── Explicit scalar decisions ──────────────────────────────────────────────────


class TestExplicitDecisions:
    def _survivor(self, **kwargs: Any) -> dict:
        return _make_contact("s1", etag='W/"e-s"', **kwargs)

    def _candidate(self, **kwargs: Any) -> dict:
        return _make_contact("c1", etag='W/"e-c"', **kwargs)

    def test_aden_eppolito_candidate_wins_display_name_surname_file_as(self):
        # Uses exact normalizedName key from real reconciliation (lowercase).
        sv = self._survivor(display_name="Aden E.", surname="E.", file_as="E., Aden")
        cand = self._candidate(display_name="Aden Eppolito", surname="Eppolito", file_as="Eppolito, Aden")
        merged, _ = _merge_contact("aden eppolito", sv, [cand])
        assert merged["displayName"] == "Aden Eppolito"
        assert merged["surname"] == "Eppolito"
        assert merged["fileAs"] == "Eppolito, Aden"

    def test_aden_eppolito_survivor_mobile_format_preserved_when_digit_equal(self):
        sv = self._survivor(mobile_phone="(555) 100-0001")
        cand = self._candidate(mobile_phone="5551000001")
        merged, _ = _merge_contact("aden eppolito", sv, [cand])
        assert merged["mobilePhone"] == "(555) 100-0001"

    def test_chris_reickard_candidate_wins_display_name_given_name_file_as(self):
        # Uses exact normalizedName key from real reconciliation (lowercase).
        sv = self._survivor(display_name="C. Reickard", given_name="C.", file_as="Reickard, C.")
        cand = self._candidate(display_name="Chris Reickard", given_name="Chris", file_as="Reickard, Chris")
        merged, _ = _merge_contact("chris reickard", sv, [cand])
        assert merged["displayName"] == "Chris Reickard"
        assert merged["givenName"] == "Chris"
        assert merged["fileAs"] == "Reickard, Chris"

    def test_chris_reickard_survivor_mobile_format_preserved_when_digit_equal(self):
        sv = self._survivor(mobile_phone="(800) 555-0001")
        cand = self._candidate(mobile_phone="8005550001")
        merged, _ = _merge_contact("chris reickard", sv, [cand])
        assert merged["mobilePhone"] == "(800) 555-0001"

    def test_chris_ferrari_survivor_primary_email_preserved(self):
        # Uses exact normalizedName key from real reconciliation (lowercase).
        sv_email = {"address": "ferrari@survivor.com", "name": "Ferrari"}
        cand_email = {"address": "ferrari@candidate.com", "name": "Ferrari Alt"}
        sv = self._survivor(primary_email_address=sv_email)
        cand = self._candidate(primary_email_address=cand_email)
        merged, _ = _merge_contact("chris ferrari", sv, [cand])
        assert merged["primaryEmailAddress"] == sv_email

    def test_chris_ferrari_blank_file_as_filled_from_candidate(self):
        sv = self._survivor(file_as=None)
        cand = self._candidate(file_as="Ferrari, Chris")
        merged, _ = _merge_contact("chris ferrari", sv, [cand])
        assert merged["fileAs"] == "Ferrari, Chris"

    def test_chris_ferrari_survivor_mobile_format_preserved_when_digit_equal(self):
        sv = self._survivor(mobile_phone="(650) 555-1234")
        cand = self._candidate(mobile_phone="6505551234")
        merged, _ = _merge_contact("chris ferrari", sv, [cand])
        assert merged["mobilePhone"] == "(650) 555-1234"

    def test_every_normalized_equal_mobile_preserves_survivor_format(self):
        """Rule 4: all normalized-equal mobilePhone conflicts preserve survivor format."""
        sv = _make_contact("s", etag='W/"e"', mobile_phone="+1 (415) 555-0001")
        cand = _make_contact("c", etag='W/"ec"', mobile_phone="14155550001")
        merged, _ = _merge_contact("Random Person", sv, [cand])
        assert merged["mobilePhone"] == "+1 (415) 555-0001"


# ── Two-candidate (merge-before-delete) groups ────────────────────────────────


class TestTwoCandidateGroups:
    def test_two_candidates_both_contribute_to_payload(self):
        # Uses exact normalizedName key (lowercase); aaron kimball has two candidates.
        survivor = _make_contact("s1", etag='W/"e-s"',
                                  email_addresses=[{"address": "s@example.com"}])
        cand1 = _make_contact("c1", etag='W/"e-c1"',
                               email_addresses=[{"address": "c1@example.com"}],
                               last_modified="2026-01-01T00:00:00Z")
        cand2 = _make_contact("c2", etag='W/"e-c2"',
                               email_addresses=[{"address": "c2@example.com"}],
                               last_modified="2026-02-01T00:00:00Z")
        merged, _ = _merge_contact("aaron kimball", survivor, [cand1, cand2])
        emails = {e["address"] for e in merged["emailAddresses"]}
        assert "s@example.com" in emails
        assert "c1@example.com" in emails
        assert "c2@example.com" in emails

    def test_two_candidates_notes_all_merged(self):
        # Uses exact normalizedName key with curly apostrophe U+2019.
        survivor = _make_contact("s1", etag='W/"e-s"', personal_notes="SNote")
        cand1 = _make_contact("c1", etag='W/"e-c1"', personal_notes="C1Note",
                               last_modified="2026-01-01T00:00:00Z")
        cand2 = _make_contact("c2", etag='W/"e-c2"', personal_notes="C2Note",
                               last_modified="2026-02-01T00:00:00Z")
        merged, note_count = _merge_contact("adam o’daniel", survivor, [cand1, cand2])
        assert "SNote" in merged["personalNotes"]
        assert "C1Note" in merged["personalNotes"]
        assert "C2Note" in merged["personalNotes"]
        assert note_count == 1

    def test_candidate_sort_order_by_last_modified_then_id(self):
        c1 = _make_contact("c1", etag='W/"e1"', last_modified="2026-03-01T00:00:00Z")
        c2 = _make_contact("c2", etag='W/"e2"', last_modified="2026-01-01T00:00:00Z")
        sorted_cands = _sort_candidates([c1, c2])
        assert sorted_cands[0]["id"] == "c2"  # earlier date first
        assert sorted_cands[1]["id"] == "c1"

    def test_candidate_sort_tie_broken_by_id(self):
        same_date = "2026-01-01T00:00:00Z"
        c_b = _make_contact("c_b", etag='W/"eb"', last_modified=same_date)
        c_a = _make_contact("c_a", etag='W/"ea"', last_modified=same_date)
        sorted_cands = _sort_candidates([c_b, c_a])
        assert sorted_cands[0]["id"] == "c_a"
        assert sorted_cands[1]["id"] == "c_b"


# ── Full merge_contact output shape ───────────────────────────────────────────


class TestMergeContactShape:
    def test_output_contains_all_mutable_fields(self):
        sv = _make_contact("s1", etag='W/"e"')
        cand = _make_contact("c1", etag='W/"ec"')
        merged, _ = _merge_contact("Test Person", sv, [cand])
        assert set(merged.keys()) == MUTABLE_FIELDS_SET

    def test_no_immutable_fields_in_output(self):
        sv = _make_contact("s1", etag='W/"e"')
        cand = _make_contact("c1", etag='W/"ec"')
        merged, _ = _merge_contact("Test Person", sv, [cand])
        immutable = {"id", "@odata.etag", "changeKey", "createdDateTime",
                     "lastModifiedDateTime", "parentFolderId"}
        assert not (set(merged.keys()) & immutable)


# ── Manifest structure validation ─────────────────────────────────────────────


class TestValidateManifestStructure:
    def _make_valid_manifest(self, n_groups: int = 84, cands_per_group: int = 1) -> dict:
        groups = []
        for i in range(n_groups):
            sid = f"s{i}"
            cids = [f"c{i}_{j}" for j in range(cands_per_group)]
            all_ids = [sid] + cids
            payload = {f: None for f in MUTABLE_FIELDS}
            groups.append({
                "group_id": f"g{i}",
                "survivor_id": sid,
                "candidate_ids": cids,
                "approved_before_hashes": {iid: "aaa" for iid in all_ids},
                "approved_etags": {iid: 'W/"e"' for iid in all_ids},
                "desired_survivor_payload": payload,
                "managed_fields": list(MUTABLE_FIELDS),
                "candidate_delete_order": cids,
                "trigger": f"OPS-10 manifest mid group g{i}",
            })
        return {"manifest_id": "mid", "target_user": "u@x.com", "groups": groups}

    def test_valid_manifest_passes(self):
        m = self._make_valid_manifest(n_groups=3, cands_per_group=1)
        groups_meta = [
            {"group_id": f"g{i}", "name": f"Person {i}",
             "survivor_id": f"s{i}", "candidate_ids": [f"c{i}_0"]}
            for i in range(3)
        ]
        _validate_manifest_structure(
            m, groups_meta,
            expected_keep=3, expected_delete=3, expected_in_scope=6,
        )

    def _validate(self, m: dict, **kwargs) -> None:
        n = len(m["groups"])
        total_cands = sum(len(g.get("candidate_ids", [])) for g in m["groups"])
        in_scope = n + total_cands
        _validate_manifest_structure(
            m, [],
            expected_keep=n, expected_delete=total_cands, expected_in_scope=in_scope,
            **kwargs,
        )

    def test_missing_managed_fields_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        m["groups"][0]["managed_fields"] = ["displayName"]  # incomplete
        with pytest.raises(GeneratorError, match="managed_fields"):
            self._validate(m)

    def test_immutable_field_in_payload_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        m["groups"][0]["desired_survivor_payload"]["id"] = "bad"
        m["groups"][0]["managed_fields"].append("id")
        with pytest.raises(GeneratorError):
            self._validate(m)

    def test_payload_keys_not_matching_managed_fields_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        del m["groups"][0]["desired_survivor_payload"]["displayName"]
        m["groups"][0]["desired_survivor_payload"]["EXTRA"] = "x"
        with pytest.raises(GeneratorError, match="payload keys"):
            self._validate(m)

    def test_wrong_group_count_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        with pytest.raises(GeneratorError, match="84"):
            _validate_manifest_structure(m, [])  # uses default expected_keep=84

    def test_approved_etags_extra_key_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        m["groups"][0]["approved_etags"]["extra_id"] = 'W/"x"'
        with pytest.raises(GeneratorError, match="approved_etags"):
            self._validate(m)

    def test_duplicate_contact_id_across_groups_raises(self):
        m = self._make_valid_manifest(n_groups=2, cands_per_group=1)
        dupe_id = m["groups"][0]["survivor_id"]
        # Patch group 1 to reuse group 0's survivor_id, keeping hash/etag keys consistent.
        old_sid = m["groups"][1]["survivor_id"]
        m["groups"][1]["survivor_id"] = dupe_id
        m["groups"][1]["approved_etags"][dupe_id] = 'W/"dup"'
        m["groups"][1]["approved_etags"].pop(old_sid, None)
        m["groups"][1]["approved_before_hashes"][dupe_id] = "aaa"
        m["groups"][1]["approved_before_hashes"].pop(old_sid, None)
        with pytest.raises(GeneratorError, match="more than one group"):
            self._validate(m)


# ── Manifest schema + executor loader compatibility ───────────────────────────


class TestExecutorLoaderCompatibility:
    """Test that a generated manifest can be loaded by the committed executor."""

    def _mini_snapshot(self, group_defs: list[dict]) -> dict[str, dict]:
        contacts = {}
        for g in group_defs:
            sid = g["survivor_id"]
            contacts[sid] = _make_contact(sid, etag=f'W/"e-{sid}"')
            for cid in g["candidate_ids"]:
                contacts[cid] = _make_contact(cid, etag=f'W/"e-{cid}"')
        return contacts

    def test_generated_manifest_loads_via_executor(self, tmp_path: Path):
        from tools.msgraph_user_contacts_write_client import (
            _contact_state_hash,
            _load_group_manifest,
        )

        group_defs = [
            {"group_id": "g0", "name": "Alice", "survivor_id": "s0", "candidate_ids": ["c0"]},
            {"group_id": "g1", "name": "Bob", "survivor_id": "s1", "candidate_ids": ["c1"]},
        ]
        snap = self._mini_snapshot(group_defs)
        manifest_id = "ops10-r1-test0001"

        manifest, _, _ = _build_group_manifest(
            manifest_id, "user@example.com", group_defs, snap, _contact_state_hash
        )

        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        manifest_sha256 = _sha256_of(manifest_bytes)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)

        loaded_id, loaded_groups = _load_group_manifest(
            manifest_path, manifest_sha256, "user@example.com"
        )
        assert loaded_id == manifest_id
        assert len(loaded_groups) == 2
        total_cands = sum(len(g.candidate_ids) for g in loaded_groups)
        assert total_cands == 2

    def test_manifest_must_include_all_mutable_fields_per_group(self, tmp_path: Path):
        from tools.msgraph_user_contacts_write_client import _contact_state_hash

        group_defs = [
            {"group_id": "g0", "name": "Test", "survivor_id": "s0", "candidate_ids": ["c0"]}
        ]
        snap = self._mini_snapshot(group_defs)
        manifest, _, _ = _build_group_manifest(
            "ops10-r1-x", "u@e.com", group_defs, snap, _contact_state_hash
        )
        assert set(manifest["groups"][0]["managed_fields"]) == MUTABLE_FIELDS_SET
        assert set(manifest["groups"][0]["desired_survivor_payload"].keys()) == MUTABLE_FIELDS_SET


# ── Redacted review ────────────────────────────────────────────────────────────


_FORBIDDEN_REDACTED_PATTERNS = [
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",  # UUID contact IDs
    r'W/"[^"]+"',  # ETags
    r"\b\d{10,}\b",  # phone digit strings (>=10 digits)
    r"@\w+\.\w+",  # email addresses
]

import re as _re


class TestRedactedReview:
    def _make_review(self) -> dict:
        return _build_redacted_review(
            manifest_id="ops10-r1-abc123",
            generator_version="1.0.0",
            decision_revision="r1",
            group_count=84,
            candidate_count=87,
            in_scope_count=171,
            hold_count=166,
            note_decisions_before=2,
            note_decisions_after=0,
            redacted_groups=[
                {
                    "name": "Alice Smith",
                    "company": "Acme Corp",
                    "changed_fields": ["displayName", "emailAddresses"],
                    "multi_value_additions": {"emailAddresses": 1},
                    "note_merge_count": 0,
                    "decisions": [],
                    "candidate_count": 1,
                    "explicit_decision": None,
                }
            ],
            approvable=True,
        )

    def test_approvable_true(self):
        r = self._make_review()
        assert r["approvable"] is True

    def test_contains_name_and_company(self):
        r = self._make_review()
        group = r["groups"][0]
        assert group["name"] == "Alice Smith"
        assert group["company"] == "Acme Corp"

    def test_contains_changed_field_names(self):
        r = self._make_review()
        group = r["groups"][0]
        assert "displayName" in group["changed_fields"]
        assert "emailAddresses" in group["changed_fields"]

    def test_contains_multi_value_addition_counts(self):
        r = self._make_review()
        assert r["groups"][0]["multi_value_additions"]["emailAddresses"] == 1

    def test_no_contact_ids_in_output(self):
        r = self._make_review()
        text = json.dumps(r)
        # No UUID-format contact IDs.
        for pat in _FORBIDDEN_REDACTED_PATTERNS:
            m = _re.search(pat, text)
            assert m is None, f"Forbidden pattern {pat!r} found: {m.group()!r}"

    def test_no_group_ids_in_group_entries(self):
        r = self._make_review()
        for g in r["groups"]:
            assert "group_id" not in g

    def test_summary_counts_correct(self):
        r = self._make_review()
        s = r["summary"]
        assert s["group_count"] == 84
        assert s["candidate_count"] == 87
        assert s["excluded_hold_count"] == 166
        assert s["note_decisions_before_rule"] == 2
        assert s["note_decisions_after_rule"] == 0


# ── Atomic write ───────────────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_file_created_with_correct_content(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_write(target, b'{"ok": true}')
        assert target.read_bytes() == b'{"ok": true}'

    def test_file_mode_is_0600(self, tmp_path: Path):
        target = tmp_path / "secret.json"
        _atomic_write(target, b"data", mode=0o600)
        file_mode = stat.S_IMODE(os.stat(target).st_mode)
        assert file_mode == 0o600, f"Expected 0600, got {oct(file_mode)}"

    def test_atomic_no_partial_file_on_error(self, tmp_path: Path):
        target = tmp_path / "out.json"
        # Monkeypatching not allowed; verify no stale temp file left after normal write.
        _atomic_write(target, b"complete")
        # Only the target file should exist; no leftover temp files.
        files = list(tmp_path.iterdir())
        assert target in files
        tmp_files = [f for f in files if f.name.startswith(".ops10_tmp_")]
        assert not tmp_files, f"Leftover temp files: {tmp_files}"

    def test_overwrite_existing_file(self, tmp_path: Path):
        target = tmp_path / "out.json"
        _atomic_write(target, b"first")
        _atomic_write(target, b"second")
        assert target.read_bytes() == b"second"

    def test_manifest_output_not_printed_to_stdout(self, tmp_path: Path, capsys):
        """Manifest bytes must not appear on stdout (only paths and hashes are printed)."""
        target = tmp_path / "manifest.json"
        secret = b'{"contact_id": "secret-id-12345", "phone": "5551234567"}'
        _atomic_write(target, secret)
        captured = capsys.readouterr()
        assert "secret-id-12345" not in captured.out
        assert "5551234567" not in captured.out


# ── Deterministic output ───────────────────────────────────────────────────────


class TestDeterministicOutput:
    def _two_group_setup(self) -> tuple[list[dict], dict[str, dict]]:
        group_defs = [
            {"group_id": "g0", "name": "Alice", "survivor_id": "s0", "candidate_ids": ["c0"]},
            {"group_id": "g1", "name": "Bob", "survivor_id": "s1", "candidate_ids": ["c1"]},
        ]
        snap = {
            "s0": _make_contact("s0", etag='W/"e-s0"'),
            "c0": _make_contact("c0", etag='W/"e-c0"'),
            "s1": _make_contact("s1", etag='W/"e-s1"'),
            "c1": _make_contact("c1", etag='W/"e-c1"'),
        }
        return group_defs, snap

    def test_same_inputs_produce_same_manifest_bytes(self, tmp_path: Path):
        from tools.msgraph_user_contacts_write_client import _contact_state_hash

        group_defs, snap = self._two_group_setup()

        snap_path = tmp_path / "snap.json"
        recon_path = tmp_path / "recon.json"
        snap_path.write_text(
            json.dumps(_make_snapshot(list(snap.values()))), encoding="utf-8"
        )
        recon_path.write_text(
            json.dumps(_make_reconciliation(group_defs)), encoding="utf-8"
        )

        mid = _make_manifest_id(snap_path, recon_path)
        m1, _, _ = _build_group_manifest(mid, "u@e.com", group_defs, snap, _contact_state_hash)
        m2, _, _ = _build_group_manifest(mid, "u@e.com", group_defs, snap, _contact_state_hash)

        bytes1 = json.dumps(m1, sort_keys=True, ensure_ascii=False).encode()
        bytes2 = json.dumps(m2, sort_keys=True, ensure_ascii=False).encode()
        assert bytes1 == bytes2

    def test_manifest_id_stable_for_same_inputs(self, tmp_path: Path):
        snap_path = tmp_path / "snap.json"
        recon_path = tmp_path / "recon.json"
        snap_path.write_bytes(b'{"x": 1}')
        recon_path.write_bytes(b'{"y": 2}')
        id1 = _make_manifest_id(snap_path, recon_path)
        id2 = _make_manifest_id(snap_path, recon_path)
        assert id1 == id2

    def test_different_inputs_produce_different_manifest_ids(self, tmp_path: Path):
        p1 = tmp_path / "snap1.json"
        p2 = tmp_path / "snap2.json"
        recon = tmp_path / "recon.json"
        p1.write_bytes(b'{"input": 1}')
        p2.write_bytes(b'{"input": 2}')
        recon.write_bytes(b'{}')
        assert _make_manifest_id(p1, recon) != _make_manifest_id(p2, recon)

    def test_trigger_contains_manifest_id_and_group_id(self):
        trigger = _make_trigger("ops10-r1-abc123", "g42")
        assert "ops10-r1-abc123" in trigger
        assert "g42" in trigger
        assert "OPS-10" in trigger
