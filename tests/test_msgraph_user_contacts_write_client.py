"""Tests for MicrosoftGraphUserContactsWriteClient and Ops10GroupExecutor.

All tests are hermetic — no live Microsoft endpoints, no credentials, no
Keychain, no certificate store access.  HTTP is routed through SpyTransport;
audit appends use a real tmp_path directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import tools.write_audit_log as write_audit_log
from tests.fakes.write_audit_http import SpyTransport, json_response
from tools.msgraph_write_client import DEFAULT_GRAPH_BASE_URL, MicrosoftGraphContactsWriteClient
from tools.msgraph_user_contacts_write_client import (
    BeforeStateMismatchError,
    ETagMismatchError,
    ManifestError,
    MicrosoftGraphUserContactsWriteClient,
    Ops10GroupExecutor,
    PartialStateError,
    VerificationError,
    _contact_state_hash,
    _sha256_hex,
)
from tools.sync_json_http import HttpResponse
from tools.write_audit_log import (
    AFTER_UNKNOWN,
    MissingWriteTriggerError,
    WriteAuditLogError,
    WriteAuditOutcomeLogError,
    iter_entries,
)

TARGET_USER = "user@example.com"
ENCODED_USER = "user%40example.com"
TRIGGER = "OPS-10 merge 2026-08-25 run-abc"

CONTACT_ETAG = 'W/"etag-aaa"'
CONTACT_BEFORE = {
    "@odata.etag": CONTACT_ETAG,
    "id": "cid-1",
    "displayName": "Old Name",
    "emailAddresses": [{"address": "old@example.com"}],
}
CONTACT_AFTER = {
    "@odata.etag": 'W/"etag-aaa-patched"',
    "id": "cid-1",
    "displayName": "New Name",
    "emailAddresses": [{"address": "new@example.com"}],
}


def _hash(contact: Any) -> str:
    return _contact_state_hash(contact)


def _sha256_of(data: bytes) -> str:
    return _sha256_hex(data)


def entries(log_dir: Path) -> list[dict]:
    return [e for _, _, e in iter_entries(log_dir)]


def intent_entries(log_dir: Path) -> list[dict]:
    return [e for e in entries(log_dir) if e["audit_phase"] == "intent"]


def outcome_entries(log_dir: Path) -> list[dict]:
    return [e for e in entries(log_dir) if e["audit_phase"] == "outcome"]


# ── Manifest fixtures ─────────────────────────────────────────────────────────


def _write_manifest(
    tmp_path: Path,
    *,
    target_user: str = TARGET_USER,
    entries_list: list[dict] | None = None,
) -> tuple[Path, str]:
    """Write a contact manifest JSON, return (path, sha256_hex)."""
    if entries_list is None:
        entries_list = [
            {
                "contact_id": "cid-1",
                "operation": "update",
                "approved_etag": CONTACT_ETAG,
                "approved_before_hash": _hash(CONTACT_BEFORE),
                "payload": {"displayName": "New Name", "emailAddresses": [{"address": "new@example.com"}]},
                "managed_fields": ["displayName", "emailAddresses"],
                "trigger": TRIGGER,
            }
        ]
    manifest = {
        "manifest_id": "manifest-001",
        "run_id": "run-abc",
        "target_user": target_user,
        "entries": entries_list,
    }
    raw = json.dumps(manifest).encode("utf-8")
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    return path, _sha256_of(raw)


def _user_client(
    transport: SpyTransport,
    log_dir: Path,
    manifest_path: Path,
    manifest_sha256: str,
    **kwargs: Any,
) -> MicrosoftGraphUserContactsWriteClient:
    return MicrosoftGraphUserContactsWriteClient(
        lambda: "fake-token",
        target_user=TARGET_USER,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _: None,
        **kwargs,
    )


# ── Manifest validation ───────────────────────────────────────────────────────


class TestManifestValidation:
    def test_missing_manifest_file_raises_manifest_error(self, tmp_path, log_dir):
        with pytest.raises(ManifestError, match="not found"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=tmp_path / "nonexistent.json",
                manifest_sha256="aaaa",
                log_dir=log_dir,
            )

    def test_hash_mismatch_raises_manifest_error(self, tmp_path, log_dir):
        path, _ = _write_manifest(tmp_path)
        with pytest.raises(ManifestError, match="SHA-256"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256="0" * 64,  # wrong hash
                log_dir=log_dir,
            )

    def test_wrong_target_user_raises_manifest_error(self, tmp_path, log_dir):
        path, sha256 = _write_manifest(tmp_path, target_user="other@example.com")
        with pytest.raises(ManifestError, match="target_user"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_duplicate_contact_ids_raises_manifest_error(self, tmp_path, log_dir):
        entry = {
            "contact_id": "cid-1",
            "operation": "delete",
            "approved_etag": CONTACT_ETAG,
            "approved_before_hash": _hash(CONTACT_BEFORE),
            "trigger": TRIGGER,
        }
        path, sha256 = _write_manifest(tmp_path, entries_list=[entry, entry])
        with pytest.raises(ManifestError, match="[Dd]uplicate"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_create_operation_rejected(self, tmp_path, log_dir):
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[
                {
                    "contact_id": "cid-1",
                    "operation": "create",
                    "approved_etag": CONTACT_ETAG,
                    "approved_before_hash": _hash(CONTACT_BEFORE),
                    "trigger": TRIGGER,
                }
            ],
        )
        with pytest.raises(ManifestError, match="create"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_unapproved_contact_id_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)
        with pytest.raises(ManifestError, match="not in"):
            client.update_contact("cid-99", trigger=TRIGGER)

    def test_wrong_operation_raises_manifest_error(self, tmp_path, log_dir, transport):
        """Contact is approved for 'update' but 'delete' is requested."""
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)
        with pytest.raises(ManifestError, match="update.*delete|delete.*update"):
            client.delete_contact("cid-1", trigger=TRIGGER)

    def test_manifest_and_trigger_checks_precede_http_and_audit(
        self, tmp_path, log_dir, transport
    ):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        with pytest.raises(ManifestError):
            client.update_contact("cid-99", trigger=TRIGGER)

        assert transport.calls == []
        assert entries(log_dir) == []


# ── Trigger enforcement ───────────────────────────────────────────────────────


class TestTriggerEnforcement:
    def test_blank_trigger_raises_before_http_and_audit(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)
        with pytest.raises(MissingWriteTriggerError):
            client.update_contact("cid-1", trigger="")
        assert transport.calls == []
        assert entries(log_dir) == []


# ── ETag and before-state verification ───────────────────────────────────────


class TestETagAndBeforeStateVerification:
    def test_missing_etag_rejects_before_audit(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        contact_no_etag = {k: v for k, v in CONTACT_BEFORE.items() if k != "@odata.etag"}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", contact_no_etag)

        with pytest.raises(ETagMismatchError):
            client.update_contact("cid-1", trigger=TRIGGER)
        assert entries(log_dir) == []

    def test_etag_mismatch_rejects_before_audit(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        different_etag = {**CONTACT_BEFORE, "@odata.etag": 'W/"different-etag"'}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", different_etag)

        with pytest.raises(ETagMismatchError):
            client.update_contact("cid-1", trigger=TRIGGER)
        assert entries(log_dir) == []

    def test_before_state_hash_mismatch_rejects_before_audit(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        # Same ETag but different content → hash mismatch.
        drifted = {**CONTACT_BEFORE, "displayName": "Drifted Name"}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", drifted)

        with pytest.raises(BeforeStateMismatchError):
            client.update_contact("cid-1", trigger=TRIGGER)
        assert entries(log_dir) == []


# ── Audit gate ────────────────────────────────────────────────────────────────


class TestAuditGate:
    def test_audit_append_failure_blocks_patch(self, tmp_path, log_dir, transport, monkeypatch):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        monkeypatch.setattr(write_audit_log, "append_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

        with pytest.raises(WriteAuditLogError):
            client.update_contact("cid-1", trigger=TRIGGER)

        assert transport.writes == []

    def test_audit_append_failure_blocks_delete(self, tmp_path, log_dir, transport, monkeypatch):
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[
                {
                    "contact_id": "cid-1",
                    "operation": "delete",
                    "approved_etag": CONTACT_ETAG,
                    "approved_before_hash": _hash(CONTACT_BEFORE),
                    "trigger": TRIGGER,
                }
            ],
        )
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(204))

        monkeypatch.setattr(write_audit_log, "append_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

        with pytest.raises(WriteAuditLogError):
            client.delete_contact("cid-1", trigger=TRIGGER)

        assert transport.writes == []


# ── Happy path: update_contact ────────────────────────────────────────────────


class TestUpdateContactHappyPath:
    def test_exact_endpoint_used(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, CONTACT_AFTER)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        client.update_contact("cid-1", trigger=TRIGGER)

        assert [method for method, _ in transport.paths] == ["GET", "PATCH", "GET"]
        assert all(f"/users/{ENCODED_USER}/contacts/cid-1" in url for _, url in transport.paths)

    def test_if_match_header_present_on_patch(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, CONTACT_AFTER)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        client.update_contact("cid-1", trigger=TRIGGER)

        patch_call = next(c for c in transport.calls if c.method == "PATCH")
        assert patch_call.headers.get("If-Match") == CONTACT_ETAG

    def test_audit_intent_and_outcome_logged(self, tmp_path, log_dir, transport):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, CONTACT_AFTER)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        client.update_contact("cid-1", trigger=TRIGGER)

        logged = entries(log_dir)
        assert len(logged) == 2
        intent, outcome = logged
        assert intent["audit_phase"] == "intent"
        assert outcome["audit_phase"] == "outcome"
        assert intent["audit_id"] == outcome["audit_id"]
        assert outcome["before"] == CONTACT_BEFORE
        assert outcome["after"] == CONTACT_AFTER
        assert outcome["trigger"] == TRIGGER

    def test_existing_me_contacts_client_endpoint_unchanged(self, tmp_path, log_dir):
        """Confirm /me/contacts is not affected by the new class."""
        transport = SpyTransport(DEFAULT_GRAPH_BASE_URL)
        transport.route("GET", "/me/contacts/cid-1", CONTACT_BEFORE, CONTACT_AFTER)
        transport.route("PATCH", "/me/contacts/cid-1", CONTACT_AFTER)

        client = MicrosoftGraphContactsWriteClient(
            lambda: "fake-token",
            request_fn=transport,
            log_dir=log_dir,
            sleep=lambda _: None,
        )
        client.update_contact("cid-1", {"displayName": "New Name"}, trigger=TRIGGER)

        assert all("/me/contacts/cid-1" in url for _, url in transport.paths)
        assert not any(ENCODED_USER in url for _, url in transport.paths)


# ── Verification failure ──────────────────────────────────────────────────────


class TestVerificationFailure:
    def test_patch_post_verification_get_failure_raises_verification_error(
        self, tmp_path, log_dir, transport
    ):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256, max_retries=0)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(503, b"boom"))

        with pytest.raises(VerificationError):
            client.update_contact("cid-1", trigger=TRIGGER)

        # Outcome was still recorded (with after_fetch_failed).
        logged = outcome_entries(log_dir)
        assert len(logged) == 1
        assert logged[0].get("after_fetch_failed") is True

    def test_managed_field_mismatch_raises_verification_error(
        self, tmp_path, log_dir, transport
    ):
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        wrong_after = {**CONTACT_AFTER, "displayName": "WRONG NAME"}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, wrong_after)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        with pytest.raises(VerificationError, match="displayName"):
            client.update_contact("cid-1", trigger=TRIGGER)

    def test_verification_failure_and_outcome_log_failure_surfaces_outcome_error(
        self, tmp_path, log_dir, transport, monkeypatch
    ):
        """When PATCH completes but verification fails AND outcome log fails, WriteAuditOutcomeLogError surfaces."""
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        wrong_after = {**CONTACT_AFTER, "displayName": "WRONG NAME"}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, wrong_after)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        original_append = write_audit_log.append_entry

        def _fail_outcome(entry, *, log_dir, moment):
            if entry.get("audit_phase") == "outcome":
                raise OSError("outcome log fail")
            return original_append(entry, log_dir=log_dir, moment=moment)

        monkeypatch.setattr(write_audit_log, "append_entry", _fail_outcome)

        with pytest.raises(WriteAuditOutcomeLogError) as exc_info:
            client.update_contact("cid-1", trigger=TRIGGER)

        assert exc_info.value.write_completed is True
        assert len([c for c in transport.calls if c.method == "PATCH"]) == 1

    def test_outcome_not_retried_after_write_completed(self, tmp_path, log_dir, transport, monkeypatch):
        """WriteAuditOutcomeLogError.write_completed is True — must not trigger a retry."""
        path, sha256 = _write_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE, CONTACT_AFTER)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_AFTER)

        calls: list[int] = []

        original_append = write_audit_log.append_entry

        def _counting_append(entry, *, log_dir, moment):
            calls.append(1)
            if entry.get("audit_phase") == "outcome":
                raise OSError("outcome fail")
            return original_append(entry, log_dir=log_dir, moment=moment)

        monkeypatch.setattr(write_audit_log, "append_entry", _counting_append)

        with pytest.raises(WriteAuditOutcomeLogError) as exc_info:
            client.update_contact("cid-1", trigger=TRIGGER)

        assert exc_info.value.write_completed is True
        # The PATCH happened exactly once — no retry.
        assert len([c for c in transport.calls if c.method == "PATCH"]) == 1


# ── Happy path: delete_contact ────────────────────────────────────────────────


def _delete_manifest(tmp_path: Path, contact: dict = CONTACT_BEFORE) -> tuple[Path, str]:
    return _write_manifest(
        tmp_path,
        entries_list=[
            {
                "contact_id": contact["id"],
                "operation": "delete",
                "approved_etag": contact["@odata.etag"],
                "approved_before_hash": _hash(contact),
                "trigger": TRIGGER,
            }
        ],
    )


class TestDeleteContactHappyPath:
    def test_exact_endpoint_and_if_match_used(self, tmp_path, log_dir, transport):
        path, sha256 = _delete_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(204))
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(404))

        client.delete_contact("cid-1", trigger=TRIGGER)

        methods = [m for m, _ in transport.paths]
        assert methods == ["GET", "DELETE", "GET"]

        del_call = next(c for c in transport.calls if c.method == "DELETE")
        assert del_call.headers.get("If-Match") == CONTACT_ETAG

    def test_404_verified_after_delete(self, tmp_path, log_dir, transport):
        path, sha256 = _delete_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(204))
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(404))

        result = client.delete_contact("cid-1", trigger=TRIGGER)
        assert result == CONTACT_BEFORE

        outcome = outcome_entries(log_dir)[0]
        assert outcome["after"] is None
        assert outcome["operation"] == "delete"

    def test_200_after_delete_raises_verification_error(self, tmp_path, log_dir, transport):
        path, sha256 = _delete_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(204))
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)

        with pytest.raises(VerificationError, match="200"):
            client.delete_contact("cid-1", trigger=TRIGGER)

    def test_delete_verification_failure_and_outcome_log_failure_surfaces_outcome_error(
        self, tmp_path, log_dir, transport, monkeypatch
    ):
        """When DELETE completes but 404 verification fails AND outcome log fails, WriteAuditOutcomeLogError surfaces."""
        path, sha256 = _delete_manifest(tmp_path)
        client = _user_client(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cid-1", HttpResponse(204))
        # Returns 200 instead of 404 — verification fails.
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cid-1", CONTACT_BEFORE)

        original_append = write_audit_log.append_entry

        def _fail_outcome(entry, *, log_dir, moment):
            if entry.get("audit_phase") == "outcome":
                raise OSError("outcome log fail")
            return original_append(entry, log_dir=log_dir, moment=moment)

        monkeypatch.setattr(write_audit_log, "append_entry", _fail_outcome)

        with pytest.raises(WriteAuditOutcomeLogError) as exc_info:
            client.delete_contact("cid-1", trigger=TRIGGER)

        assert exc_info.value.write_completed is True
        assert len([c for c in transport.calls if c.method == "DELETE"]) == 1


# ── Ops10GroupExecutor ────────────────────────────────────────────────────────


SURVIVOR = {
    "@odata.etag": 'W/"etag-survivor"',
    "id": "sur-1",
    "displayName": "Merged Name",
    "emailAddresses": [{"address": "merged@example.com"}],
    "notes": "old note",
}
DESIRED_SURVIVOR = {
    "displayName": "Merged Name",
    "emailAddresses": [{"address": "merged@example.com"}],
    "notes": "combined note",
}
SURVIVOR_AFTER_PATCH = {
    "@odata.etag": 'W/"etag-survivor-v2"',
    "id": "sur-1",
    "displayName": "Merged Name",
    "emailAddresses": [{"address": "merged@example.com"}],
    "notes": "combined note",
}
CAND_A = {
    "@odata.etag": 'W/"etag-cand-a"',
    "id": "cand-a",
    "displayName": "Candidate A",
}
CAND_B = {
    "@odata.etag": 'W/"etag-cand-b"',
    "id": "cand-b",
    "displayName": "Candidate B",
}


def _write_group_manifest(
    tmp_path: Path,
    *,
    target_user: str = TARGET_USER,
    groups: list[dict] | None = None,
) -> tuple[Path, str]:
    if groups is None:
        groups = [
            {
                "group_id": "grp-1",
                "trigger": TRIGGER,
                "survivor_id": "sur-1",
                "candidate_ids": ["cand-a", "cand-b"],
                "approved_before_hashes": {
                    "sur-1": _hash(SURVIVOR),
                    "cand-a": _hash(CAND_A),
                    "cand-b": _hash(CAND_B),
                },
                "approved_etags": {
                    "sur-1": SURVIVOR["@odata.etag"],
                    "cand-a": CAND_A["@odata.etag"],
                    "cand-b": CAND_B["@odata.etag"],
                },
                "desired_survivor_payload": DESIRED_SURVIVOR,
                "managed_fields": ["displayName", "emailAddresses", "notes"],
                "candidate_delete_order": ["cand-a", "cand-b"],
            }
        ]
    manifest = {
        "manifest_id": "grp-manifest-001",
        "run_id": "run-grp",
        "target_user": target_user,
        "groups": groups,
    }
    raw = json.dumps(manifest).encode("utf-8")
    path = tmp_path / "group_manifest.json"
    path.write_bytes(raw)
    return path, _sha256_of(raw)


def _executor(
    transport: SpyTransport,
    log_dir: Path,
    manifest_path: Path,
    manifest_sha256: str,
    **kwargs: Any,
) -> Ops10GroupExecutor:
    return Ops10GroupExecutor(
        lambda: "fake-token",
        target_user=TARGET_USER,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        request_fn=transport,
        log_dir=log_dir,
        sleep=lambda _: None,
        **kwargs,
    )


def _route_happy_group(transport: SpyTransport) -> None:
    """Register all routes for the standard two-candidate happy path."""
    transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR, SURVIVOR_AFTER_PATCH)
    transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR_AFTER_PATCH)
    transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
    transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(204))
    transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(404))
    transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)
    transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cand-b", HttpResponse(204))
    transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", HttpResponse(404))


class TestOps10GroupExecutorManifest:
    def test_hash_mismatch_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, _ = _write_group_manifest(tmp_path)
        with pytest.raises(ManifestError, match="SHA-256"):
            _executor(transport, log_dir, path, "0" * 64)

    def test_wrong_target_user_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path, target_user="other@example.com")
        with pytest.raises(ManifestError, match="target_user"):
            _executor(transport, log_dir, path, sha256)

    def test_duplicate_contact_ids_across_groups_raises_manifest_error(self, tmp_path, log_dir, transport):
        # Use a single-key payload/managed_fields so exactness checks pass and
        # the duplicate-ID check is the first ManifestError triggered.
        simple_payload = {"displayName": "Merged Name"}
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[
                {
                    "group_id": "grp-1",
                    "trigger": TRIGGER,
                    "survivor_id": "sur-1",
                    "candidate_ids": ["cand-a"],
                    "approved_before_hashes": {"sur-1": _hash(SURVIVOR), "cand-a": _hash(CAND_A)},
                    "approved_etags": {"sur-1": SURVIVOR["@odata.etag"], "cand-a": CAND_A["@odata.etag"]},
                    "desired_survivor_payload": simple_payload,
                    "managed_fields": ["displayName"],
                    "candidate_delete_order": ["cand-a"],
                },
                {
                    "group_id": "grp-2",
                    "trigger": TRIGGER,
                    "survivor_id": "sur-1",  # duplicate!
                    "candidate_ids": ["cand-b"],
                    "approved_before_hashes": {"sur-1": _hash(SURVIVOR), "cand-b": _hash(CAND_B)},
                    "approved_etags": {"sur-1": SURVIVOR["@odata.etag"], "cand-b": CAND_B["@odata.etag"]},
                    "desired_survivor_payload": simple_payload,
                    "managed_fields": ["displayName"],
                    "candidate_delete_order": ["cand-b"],
                },
            ],
        )
        with pytest.raises(ManifestError, match="[Dd]uplicate|multiple groups"):
            _executor(transport, log_dir, path, sha256)


class TestOps10GroupExecutorHappyPath:
    def test_one_patch_before_both_deletes(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)
        _route_happy_group(transport)

        ex.execute_group("grp-1")

        methods = [m for m, _ in transport.paths]
        # Before any DELETE: at least one PATCH must appear.
        patch_idx = methods.index("PATCH")
        first_delete_idx = methods.index("DELETE")
        assert patch_idx < first_delete_idx

    def test_audit_intent_before_patch_and_each_delete(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)
        _route_happy_group(transport)

        ex.execute_group("grp-1")

        intents = intent_entries(log_dir)
        outcomes = outcome_entries(log_dir)
        # One PATCH + two DELETEs = 3 intent/outcome pairs.
        assert len(intents) == 3
        assert len(outcomes) == 3

    def test_each_candidate_delete_has_own_audit_id(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)
        _route_happy_group(transport)

        ex.execute_group("grp-1")

        delete_intents = [e for e in intent_entries(log_dir) if e["operation"] == "delete"]
        assert len(delete_intents) == 2
        audit_ids = [e["audit_id"] for e in delete_intents]
        assert len(set(audit_ids)) == 2, "Each delete must have a distinct audit_id"

    def test_if_match_on_patch_and_each_delete(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)
        _route_happy_group(transport)

        ex.execute_group("grp-1")

        patch_call = next(c for c in transport.calls if c.method == "PATCH")
        assert patch_call.headers.get("If-Match") == SURVIVOR["@odata.etag"]

        del_calls = [c for c in transport.calls if c.method == "DELETE"]
        assert len(del_calls) == 2
        del_etags = {c.headers.get("If-Match") for c in del_calls}
        assert CAND_A["@odata.etag"] in del_etags
        assert CAND_B["@odata.etag"] in del_etags

    def test_404_verified_after_each_delete(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)
        _route_happy_group(transport)

        ex.execute_group("grp-1")

        # Two 404 GETs should appear (one per candidate).
        get_calls = [c for c in transport.calls if c.method == "GET"]
        # The final two GETs after each delete return 404 (per SpyTransport routing).
        assert len(get_calls) >= 5  # pre-GET survivor + pre-GET candidates + post-patch GET + 2 verify GETs

    def test_survivor_verification_before_any_delete(self, tmp_path, log_dir, transport):
        """If the survivor managed-field check fails, no candidate is deleted."""
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        # Survivor PATCH returns wrong after state.
        bad_after = {**SURVIVOR_AFTER_PATCH, "displayName": "WRONG", "notes": "WRONG"}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR, bad_after)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/sur-1", bad_after)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)

        with pytest.raises(VerificationError):
            ex.execute_group("grp-1")

        assert transport.writes == [] or all(c.method == "PATCH" for c in transport.writes)
        assert len([c for c in transport.calls if c.method == "DELETE"]) == 0


class TestOps10GroupExecutorDriftDetection:
    def test_etag_mismatch_hard_stops(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        drifted = {**SURVIVOR, "@odata.etag": 'W/"different-etag"'}
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", drifted)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)

        with pytest.raises(ETagMismatchError):
            ex.execute_group("grp-1")

        assert transport.writes == []

    def test_before_hash_mismatch_hard_stops(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        drifted = {**SURVIVOR, "displayName": "Drifted Content"}  # same ETag, different hash
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", drifted)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)

        with pytest.raises(BeforeStateMismatchError):
            ex.execute_group("grp-1")

        assert transport.writes == []


class TestOps10IdempotencyAndPartialState:
    def test_survivor_already_correct_skips_patch(self, tmp_path, log_dir, transport):
        """If survivor already matches desired payload, PATCH is skipped."""
        # Survivor already has the desired content for all managed fields.
        already_correct = {**SURVIVOR, **DESIRED_SURVIVOR}
        # Update hash to match the "already correct" state.
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[
                {
                    "group_id": "grp-1",
                    "trigger": TRIGGER,
                    "survivor_id": "sur-1",
                    "candidate_ids": ["cand-a"],
                    "approved_before_hashes": {
                        "sur-1": _hash(already_correct),
                        "cand-a": _hash(CAND_A),
                    },
                    "approved_etags": {
                        "sur-1": already_correct["@odata.etag"],
                        "cand-a": CAND_A["@odata.etag"],
                    },
                    "desired_survivor_payload": DESIRED_SURVIVOR,
                    "managed_fields": ["displayName", "emailAddresses", "notes"],
                    "candidate_delete_order": ["cand-a"],
                }
            ],
        )
        ex = _executor(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", already_correct)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
        transport.route("DELETE", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(204))
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(404))

        ex.execute_group("grp-1")

        assert not any(c.method == "PATCH" for c in transport.calls)

    def test_absent_candidate_with_correct_survivor_is_no_op(self, tmp_path, log_dir, transport):
        """Absent candidate + correct survivor = no DELETE emitted for that candidate."""
        already_correct = {**SURVIVOR, **DESIRED_SURVIVOR}
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[
                {
                    "group_id": "grp-1",
                    "trigger": TRIGGER,
                    "survivor_id": "sur-1",
                    "candidate_ids": ["cand-a"],
                    "approved_before_hashes": {
                        "sur-1": _hash(already_correct),
                        "cand-a": _hash(CAND_A),
                    },
                    "approved_etags": {
                        "sur-1": already_correct["@odata.etag"],
                        "cand-a": CAND_A["@odata.etag"],
                    },
                    "desired_survivor_payload": DESIRED_SURVIVOR,
                    "managed_fields": ["displayName", "emailAddresses", "notes"],
                    "candidate_delete_order": ["cand-a"],
                }
            ],
        )
        ex = _executor(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", already_correct)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(404))

        ex.execute_group("grp-1")

        # No writes at all — both PATCH and DELETE were no-ops.
        assert transport.writes == []
        assert entries(log_dir) == []

    def test_absent_candidate_with_incorrect_survivor_is_partial_state(self, tmp_path, log_dir, transport):
        """Absent candidate + incorrect survivor = PartialStateError."""
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", HttpResponse(404))
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)

        with pytest.raises(PartialStateError):
            ex.execute_group("grp-1")

        assert transport.writes == []


class TestOps10AuditGate:
    def test_audit_append_failure_blocks_patch(self, tmp_path, log_dir, transport, monkeypatch):
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        transport.route("GET", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-a", CAND_A)
        transport.route("GET", f"/users/{ENCODED_USER}/contacts/cand-b", CAND_B)
        transport.route("PATCH", f"/users/{ENCODED_USER}/contacts/sur-1", SURVIVOR_AFTER_PATCH)

        monkeypatch.setattr(
            write_audit_log, "append_entry",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(WriteAuditLogError):
            ex.execute_group("grp-1")

        assert transport.writes == []

    def test_intent_without_outcome_hard_stops_before_http(self, tmp_path, log_dir, transport):
        """Prior intent with no outcome for a group contact ID must hard-stop before any HTTP."""
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        # Write an intent without an outcome for the survivor contact ID.
        from tools.write_audit_log import WriteAuditRecorder, MSGRAPH_CONTACTS
        recorder = WriteAuditRecorder(
            destination=MSGRAPH_CONTACTS,
            actor="prior-run-actor",
            log_dir=log_dir,
        )
        recorder.authorize_write(
            operation="update",
            record_id="sur-1",
            before=SURVIVOR,
            trigger=TRIGGER,
        )

        with pytest.raises(PartialStateError, match="intent"):
            ex.execute_group("grp-1")

        # No HTTP call was made — hard-stop before network I/O.
        assert transport.calls == []

    def test_intent_without_outcome_for_candidate_hard_stops(self, tmp_path, log_dir, transport):
        """Incomplete intent for a candidate ID also triggers a hard-stop."""
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        from tools.write_audit_log import WriteAuditRecorder, MSGRAPH_CONTACTS
        recorder = WriteAuditRecorder(
            destination=MSGRAPH_CONTACTS,
            actor="prior-run-actor",
            log_dir=log_dir,
        )
        recorder.authorize_write(
            operation="delete",
            record_id="cand-a",
            before=CAND_A,
            trigger=TRIGGER,
        )

        with pytest.raises(PartialStateError):
            ex.execute_group("grp-1")

        assert transport.calls == []

    def test_completed_intent_does_not_block_retry(self, tmp_path, log_dir, transport):
        """An intent that has a matching outcome does not trigger a hard-stop."""
        path, sha256 = _write_group_manifest(tmp_path)
        ex = _executor(transport, log_dir, path, sha256)

        # Write a completed (intent + outcome) pair — this must not block.
        from tools.write_audit_log import WriteAuditRecorder, MSGRAPH_CONTACTS
        recorder = WriteAuditRecorder(
            destination=MSGRAPH_CONTACTS,
            actor="prior-run-actor",
            log_dir=log_dir,
        )
        authorized = recorder.authorize_write(
            operation="update",
            record_id="sur-1",
            before=SURVIVOR,
            trigger=TRIGGER,
        )
        authorized.record_outcome(after=SURVIVOR_AFTER_PATCH)

        # Group should proceed to HTTP (route a fresh happy path).
        _route_happy_group(transport)
        # Must not raise PartialStateError.
        ex.execute_group("grp-1")


# ── Group manifest exactness ──────────────────────────────────────────────────


def _one_candidate_group(
    *,
    managed_fields: list[str] | None = None,
    desired_payload: dict | None = None,
    extra_before_hash_key: str | None = None,
    extra_etag_key: str | None = None,
) -> dict:
    """Build a minimal one-candidate group dict, with overrideable exactness fields."""
    mf = managed_fields if managed_fields is not None else ["displayName", "emailAddresses", "notes"]
    dp = desired_payload if desired_payload is not None else DESIRED_SURVIVOR
    before_hashes = {
        "sur-1": _hash(SURVIVOR),
        "cand-a": _hash(CAND_A),
    }
    etags = {
        "sur-1": SURVIVOR["@odata.etag"],
        "cand-a": CAND_A["@odata.etag"],
    }
    if extra_before_hash_key:
        before_hashes[extra_before_hash_key] = "extra-hash"
    if extra_etag_key:
        etags[extra_etag_key] = "extra-etag"
    return {
        "group_id": "grp-1",
        "trigger": TRIGGER,
        "survivor_id": "sur-1",
        "candidate_ids": ["cand-a"],
        "approved_before_hashes": before_hashes,
        "approved_etags": etags,
        "desired_survivor_payload": dp,
        "managed_fields": mf,
        "candidate_delete_order": ["cand-a"],
    }


class TestGroupManifestExactness:
    def test_extra_key_in_approved_before_hashes_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[_one_candidate_group(extra_before_hash_key="unknown-id")],
        )
        with pytest.raises(ManifestError, match="extra"):
            _executor(transport, log_dir, path, sha256)

    def test_extra_key_in_approved_etags_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[_one_candidate_group(extra_etag_key="unknown-id")],
        )
        with pytest.raises(ManifestError, match="extra"):
            _executor(transport, log_dir, path, sha256)

    def test_duplicate_managed_fields_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[_one_candidate_group(managed_fields=["displayName", "displayName", "notes"])],
        )
        with pytest.raises(ManifestError, match="[Dd]uplicate"):
            _executor(transport, log_dir, path, sha256)

    def test_payload_keys_not_equal_managed_fields_raises_manifest_error(self, tmp_path, log_dir, transport):
        # Extra field in payload that is not in managed_fields.
        bad_payload = {**DESIRED_SURVIVOR, "extraField": "x"}
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[_one_candidate_group(desired_payload=bad_payload)],
        )
        with pytest.raises(ManifestError, match="managed_fields"):
            _executor(transport, log_dir, path, sha256)

    def test_immutable_field_in_managed_fields_raises_manifest_error(self, tmp_path, log_dir, transport):
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[
                _one_candidate_group(
                    managed_fields=["displayName", "id"],
                    desired_payload={"displayName": "X", "id": "x"},
                )
            ],
        )
        with pytest.raises(ManifestError, match="immutable"):
            _executor(transport, log_dir, path, sha256)

    def test_immutable_field_in_payload_raises_manifest_error(self, tmp_path, log_dir, transport):
        # managed_fields is clean but payload has immutable key (@odata.etag).
        path, sha256 = _write_group_manifest(
            tmp_path,
            groups=[
                _one_candidate_group(
                    managed_fields=["displayName", "@odata.etag"],
                    desired_payload={"displayName": "X", "@odata.etag": 'W/"x"'},
                )
            ],
        )
        with pytest.raises(ManifestError, match="immutable"):
            _executor(transport, log_dir, path, sha256)


# ── Single-contact manifest exactness ────────────────────────────────────────


def _update_entry(
    *,
    managed_fields: list[str] | None = None,
    payload: dict | None = None,
) -> dict:
    mf = managed_fields if managed_fields is not None else ["displayName", "emailAddresses"]
    pl = payload if payload is not None else {
        "displayName": "New Name",
        "emailAddresses": [{"address": "new@example.com"}],
    }
    return {
        "contact_id": "cid-1",
        "operation": "update",
        "approved_etag": CONTACT_ETAG,
        "approved_before_hash": _hash(CONTACT_BEFORE),
        "payload": pl,
        "managed_fields": mf,
        "trigger": TRIGGER,
    }


class TestContactManifestExactness:
    def test_duplicate_managed_fields_raises_manifest_error(self, tmp_path, log_dir):
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[_update_entry(managed_fields=["displayName", "displayName"])],
        )
        with pytest.raises(ManifestError, match="[Dd]uplicate"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_payload_keys_not_equal_managed_fields_raises_manifest_error(self, tmp_path, log_dir):
        bad_payload = {"displayName": "X", "emailAddresses": [], "extra": "y"}
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[_update_entry(payload=bad_payload)],
        )
        with pytest.raises(ManifestError, match="managed_fields"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_immutable_field_in_managed_fields_raises_manifest_error(self, tmp_path, log_dir):
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[_update_entry(
                managed_fields=["displayName", "id"],
                payload={"displayName": "X", "id": "cid-1"},
            )],
        )
        with pytest.raises(ManifestError, match="immutable"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )

    def test_immutable_field_in_payload_raises_manifest_error(self, tmp_path, log_dir):
        path, sha256 = _write_manifest(
            tmp_path,
            entries_list=[_update_entry(
                managed_fields=["displayName", "changeKey"],
                payload={"displayName": "X", "changeKey": "ck-1"},
            )],
        )
        with pytest.raises(ManifestError, match="immutable"):
            MicrosoftGraphUserContactsWriteClient(
                lambda: "fake-token",
                target_user=TARGET_USER,
                manifest_path=path,
                manifest_sha256=sha256,
                log_dir=log_dir,
            )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "write_audit"


@pytest.fixture
def transport() -> SpyTransport:
    return SpyTransport(DEFAULT_GRAPH_BASE_URL)
