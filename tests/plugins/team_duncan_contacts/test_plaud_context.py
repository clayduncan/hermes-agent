"""Tests for the OPS-110 bounded GHL contact context projector."""

from __future__ import annotations

from plugins.team_duncan_contacts.plaud_context import (
    APPROVED_CUSTOM_FIELD_LABELS,
    BOUNDED_CONTEXT_FIELDS,
    build_bounded_contact_context,
)

FULL_CONTACT = {
    "id": "AMYTT4eio6AxChD2UQrc",
    "firstName": "Cory",
    "lastName": "Vasquez",
    "type": "other",
    "tags": ["agent", "realtyonegroup"],
    "companyName": "Realty ONE Group",
    "email": "cory.vasquez@realtyonegroup.com",
    "phone": "+15551234567",
    "locationId": "abi5iDumIeysZCvWt99r",
    "customFields": [{"name": "loan_officer", "value": "Clay Duncan"}],
    "dateOfBirth": "1980-01-01",
}


def test_only_the_bounded_fields_are_ever_present() -> None:
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert set(ctx.keys()) == BOUNDED_CONTEXT_FIELDS


def test_email_domain_only_never_full_email() -> None:
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert ctx["email_domain"] == "realtyonegroup.com"
    assert "cory.vasquez" not in str(ctx.values())
    assert "@" not in ctx["email_domain"]


def test_phone_is_never_present_in_the_context() -> None:
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert "+15551234567" not in str(ctx.values())


def test_no_unapproved_custom_field_reaches_the_context() -> None:
    assert APPROVED_CUSTOM_FIELD_LABELS == frozenset()
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert ctx["custom_fields"] == {}


def test_unbounded_fields_like_date_of_birth_never_leak_through() -> None:
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert "dateOfBirth" not in ctx
    assert "1980-01-01" not in str(ctx.values())


def test_the_ticket_real_seam_contact_projects_as_expected() -> None:
    ctx = build_bounded_contact_context(FULL_CONTACT)
    assert ctx["contact_id"] == "AMYTT4eio6AxChD2UQrc"
    assert ctx["type"] == "other"
    assert ctx["email_domain"] == "realtyonegroup.com"
    assert ctx["company_name"] == "Realty ONE Group"


def test_missing_optional_fields_are_none_not_omitted() -> None:
    ctx = build_bounded_contact_context({"id": "c-1"})
    assert ctx["contact_id"] == "c-1"
    assert ctx["first_name"] is None
    assert ctx["last_name"] is None
    assert ctx["type"] is None
    assert ctx["company_name"] is None
    assert ctx["email_domain"] is None
    assert ctx["tags"] == []
    assert ctx["custom_fields"] == {}


def test_non_string_tags_are_dropped_defensively() -> None:
    ctx = build_bounded_contact_context({"id": "c-1", "tags": ["a", 5, None, "b"]})
    assert ctx["tags"] == ["a", "b"]


def test_email_without_at_sign_yields_no_domain() -> None:
    ctx = build_bounded_contact_context({"id": "c-1", "email": "not-an-email"})
    assert ctx["email_domain"] is None
