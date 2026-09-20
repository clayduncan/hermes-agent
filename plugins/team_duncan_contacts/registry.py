"""Core contact activation registry for the team_duncan_contacts plugin.

Invariants enforced here:
- Raw handles never appear in state, return values, or logs.
- activated_at is immutable once set; the confirmation clock is the sole source.
- Atomic persistence: all state changes land via temp-file + os.replace().
- HMAC key is loaded from a 0600 file; never emitted.
- Fail closed: startup raises if contacts exist but the key is missing or corrupt.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import logging
import os
import re
import secrets
import stat
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ghl_reader import GhlContactReader, contact_display_name, extract_handle_kinds
from .sanitizer import sanitize_output

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PLUGIN_DATA_DIR_NAME = "team_duncan_contacts"
_KEY_FILE_NAME = "hmac_key"
_STATE_FILE_NAME = "registry.json"
_KEY_LENGTH = 32  # bytes for HMAC-SHA256 key
_TOKEN_TTL_SECONDS = 300  # 5 minutes

# --- Agent-input validation patterns ---
# These reject phone numbers, emails, and Apple handles from agent-facing inputs.
_PHONE_10PLUS_RE = re.compile(
    r"""
    (?:
        (?:\+?1[\s\-\.]?)?        # optional country code
        \(?\d{3}\)?[\s\-\.]?      # area code
        \d{3}[\s\-\.]?            # prefix
        \d{4}                     # line
    )
    |
    (?:\+?\d[\d\s\-\.]{8,}\d)    # 10+ digit generic
    """,
    re.VERBOSE,
)
_DIGIT_ONLY_LONG_RE = re.compile(r"^[\d\s\-\+\.\(\)]+$")
_EMAIL_RE = re.compile(r"[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}")
_APPLE_HANDLE_RE = re.compile(r"(tel:|imessage:|icloud:)", re.IGNORECASE)


def validate_agent_name_or_id(value: str) -> str:
    """Validate that *value* is a contact name or GHL contact ID, not a handle.

    Raises InvalidInputError on phone numbers, emails, and Apple handles.
    Returns the stripped value on success.

    Phone detection: reject if digit_count >= 10 AND either no letters present
    (pure numeric/formatted string) OR explicit phone formatting chars with < 3
    letters (distinguishes "+15551234567" from alphanumeric GHL IDs).
    GHL contact IDs have letters interspersed; pure phone strings do not.
    """
    v = (value or "").strip()
    if not v:
        raise InvalidInputError("Contact name or ID must not be empty.")
    if _APPLE_HANDLE_RE.search(v):
        raise InvalidInputError(
            "Apple/iMessage handles are not accepted as contact identifiers. "
            "Provide the contact's name or their GoHighLevel contact ID."
        )
    if _EMAIL_RE.search(v):
        raise InvalidInputError(
            "Email addresses are not accepted as contact identifiers. "
            "Provide the contact's name or their GoHighLevel contact ID."
        )
    digit_count = sum(c.isdigit() for c in v)
    letter_count = sum(c.isalpha() for c in v)
    if digit_count >= 10:
        if letter_count == 0:
            # Pure digits + formatting chars only (no letters) → phone number
            raise InvalidInputError(
                "Phone numbers are not accepted as contact identifiers. "
                "Provide the contact's name or their GoHighLevel contact ID."
            )
        # Has phone-formatting chars (+, -, (, ), space) but very few letters
        phone_fmt_chars = set(v) & set("+-().  ")
        if phone_fmt_chars and letter_count < 3:
            raise InvalidInputError(
                "Phone numbers are not accepted as contact identifiers. "
                "Provide the contact's name or their GoHighLevel contact ID."
            )
    return v


# --- Handle canonicalization (inside registry boundary only) ---

def _canonicalize_phone(raw: str) -> str:
    """Normalize a phone number to +1XXXXXXXXXX for US 10-digit numbers."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}"


def _canonicalize_email(raw: str) -> str:
    return raw.strip().lower()


def _canonicalize_handle(raw: str, kind: str) -> str:
    """Return the canonical form of *raw* for the given *kind*."""
    if kind == "phone":
        return _canonicalize_phone(raw)
    if kind == "email":
        return _canonicalize_email(raw)
    return raw.strip().lower()


def _mask_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***-***-****"


def _mask_email(raw: str) -> str:
    parts = raw.split("@")
    if len(parts) == 2:
        local = parts[0][:1] + "***" if parts[0] else "***"
        return f"{local}@***.***"
    return "***@***"


def _mask_handle(raw: str, kind: str) -> str:
    if kind == "phone":
        return _mask_phone(raw)
    if kind == "email":
        return _mask_email(raw)
    return "***"


# --- Candidate label support (OPS-18, additive only) ---
#
# These helpers derive a non-PII, collision-checkable "candidate row" label
# for multiple_match review, entirely from data already captured inside the
# registry's own state at prepare/confirm time (no GHL read, no network call
# happens here). See resolve_event()'s match_outcome handling below.

#: Closed set of match_outcome values. Never affects `decision`.
MATCH_OUTCOME_ZERO = "zero_match"
MATCH_OUTCOME_MULTIPLE = "multiple_match"
MATCH_OUTCOME_UNIQUE = "unique_match"
MATCH_OUTCOME_UNAVAILABLE = "unavailable"

#: Domain-separation salt for deriving the selection-token key from the same
#: HMAC key used for phone/email matching. Keeps the two derived keys
#: cryptographically independent even though they share one root key file.
_SELECTION_TOKEN_SALT = b"team-duncan-selection-token-v1"

#: Owners this build recognizes on candidate rows. Any other value (or an
#: absent one) normalizes to "(none)" rather than being shown or withholding
#: the candidate: an owner mismatch is never a reason to hide a candidate.
_OWNER_ALLOWLIST = ("Clay Duncan", "Levi Duncan")

#: Telegram MarkdownV2 reserved characters that must be backslash-escaped
#: before a sanitized label fragment is safe to render verbatim.
_MARKDOWN_V2_RESERVED = set("_*[]()~`>#+-=|{}.!")

_LABEL_MAX_LEN = 60


def _sanitize_label_text(raw: str | None) -> str | None:
    """Sanitize free-text for candidate-label rendering.

    Order: NFKC-normalize, strip control/newline/tab chars to a single
    space, collapse whitespace runs, truncate to 60 chars, then escape every
    MarkdownV2-reserved character. Returns None for empty/absent input.
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", raw)
    chars = []
    for ch in text:
        if ch in ("\n", "\r", "\t") or unicodedata.category(ch) == "Cc":
            chars.append(" ")
        else:
            chars.append(ch)
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    if not text:
        return None
    text = text[:_LABEL_MAX_LEN]
    return "".join(
        f"\\{ch}" if ch in _MARKDOWN_V2_RESERVED else ch for ch in text
    )


def _masked_phone_last4_from_masked(masked_phone: str | None) -> str | None:
    """Extract "****1234" from an already-masked phone string, if possible."""
    if not masked_phone:
        return None
    m = re.search(r"(\d{4})$", masked_phone)
    if not m:
        return None
    return "****" + m.group(1)


def _mask_email_domain_preserving(raw_email: str) -> str | None:
    """Mask an email's local part while keeping its domain, for candidate labels.

    Distinct from `_mask_email` (used for masked_handles), which redacts the
    domain too. Only called at prepare/confirm time, before the raw handle is
    discarded; never persisted as raw text.
    """
    if not raw_email or "@" not in raw_email:
        return None
    local, _, domain = raw_email.partition("@")
    if not domain:
        return None
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _normalize_owner(raw: str | None) -> str | None:
    """Return the allowlisted owner name, or None (unassigned or unrecognized)."""
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"\s+", " ", text).strip()
    for allowed in _OWNER_ALLOWLIST:
        if text == allowed:
            return allowed
    return None


def _extract_candidate_label_fields(contact: dict[str, Any]) -> dict[str, Any]:
    """Derive candidate-label source fields from a GHL contact dict.

    Called only inside prepare_activation, on the same contact dict already
    returned by the injected reader for this call: no additional read.
    Brokerage/owner field names are this build's documented convention: flat
    optional keys on the contact dict, matching the existing firstName /
    lastName / phone / email convention in ghl_reader.py.
    """
    raw_email = (contact.get("email") or "").strip()
    return {
        "masked_email": _mask_email_domain_preserving(raw_email) if raw_email else None,
        "brokerage": (contact.get("brokerage") or "").strip() or None,
        "assigned_owner": _normalize_owner(contact.get("assignedOwner")),
    }


def _derive_selection_token_key(base_key: bytes) -> bytes:
    return _hmac_mod.new(base_key, _SELECTION_TOKEN_SALT, hashlib.sha256).digest()


def _selection_token(
    base_key: bytes, contact_id: str, source: str, source_event_id: str
) -> str:
    token_key = _derive_selection_token_key(base_key)
    msg = f"selection_token|{contact_id}|{source}|{source_event_id}"
    return _hmac_mod.new(token_key, msg.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def build_candidate_display_label(contact: dict[str, Any]) -> str:
    """Build the one canonical, pre-rendered candidate label for *contact*.

    `contact` is a stored registry contact record (as in state["contacts"][id]),
    not a GHL contact dict. Must be rendered verbatim downstream, in full,
    with no further truncation or reformatting: this is the exact string
    collision-compared against every other candidate's label.
    """
    display_name = _sanitize_label_text(contact.get("display_name"))
    masked_phone_last4 = _masked_phone_last4_from_masked(
        (contact.get("masked_labels") or {}).get("phone")
    )
    label_fields = contact.get("candidate_label_fields") or {}
    masked_email = label_fields.get("masked_email")
    brokerage = _sanitize_label_text(label_fields.get("brokerage"))
    assigned_owner = label_fields.get("assigned_owner")
    parts = [
        display_name or "(none)",
        masked_phone_last4 or "(none)",
        masked_email or "(none)",
        brokerage or "(none)",
        assigned_owner or "(none)",
    ]
    return "|".join(parts)


# --- HMAC ---

def _hmac_hex(key: bytes, canonical: str) -> str:
    return _hmac_mod.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


# --- Exceptions ---

class RegistryError(Exception):
    pass


class InvalidInputError(RegistryError):
    pass


class CorruptStateError(RegistryError):
    pass


class MissingHmacKeyError(RegistryError):
    pass


class TokenNotFoundError(RegistryError):
    pass


class ContactAlreadyActivatedError(RegistryError):
    pass


class RetiredContactError(RegistryError):
    pass


# --- Result types ---

class PrepareResult:
    def __init__(
        self,
        *,
        status: str,
        contact_name: str | None = None,
        contact_id: str | None = None,
        location_id: str | None = None,
        masked_handles: dict[str, str] | None = None,
        token: str | None = None,
        token_expires_at: str | None = None,
        reason: str | None = None,
        message: str,
    ) -> None:
        self.status = status
        self.contact_name = contact_name
        self.contact_id = contact_id
        self.location_id = location_id
        self.masked_handles = masked_handles or {}
        self.token = token
        self.token_expires_at = token_expires_at
        self.reason = reason
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.contact_name is not None:
            d["contact_name"] = self.contact_name
        if self.contact_id is not None:
            d["contact_id"] = self.contact_id
        if self.location_id is not None:
            d["location_id"] = self.location_id
        if self.masked_handles:
            d["masked_handles"] = self.masked_handles
        if self.token is not None:
            d["token"] = self.token
        if self.token_expires_at is not None:
            d["token_expires_at"] = self.token_expires_at
        if self.reason is not None:
            d["reason"] = self.reason
        return sanitize_output(d)


class ConfirmResult:
    def __init__(
        self,
        *,
        status: str,
        contact_name: str | None = None,
        contact_id: str | None = None,
        location_id: str | None = None,
        activated_at: str | None = None,
        masked_handles: dict[str, str] | None = None,
        actor: str | None = None,
        reason: str | None = None,
        message: str,
    ) -> None:
        self.status = status
        self.contact_name = contact_name
        self.contact_id = contact_id
        self.location_id = location_id
        self.activated_at = activated_at
        self.masked_handles = masked_handles or {}
        self.actor = actor
        self.reason = reason
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "message": self.message}
        if self.contact_name is not None:
            d["contact_name"] = self.contact_name
        if self.contact_id is not None:
            d["contact_id"] = self.contact_id
        if self.location_id is not None:
            d["location_id"] = self.location_id
        if self.activated_at is not None:
            d["activated_at"] = self.activated_at
        if self.masked_handles:
            d["masked_handles"] = self.masked_handles
        if self.actor is not None:
            d["actor"] = self.actor
        if self.reason is not None:
            d["reason"] = self.reason
        return sanitize_output(d)


class ResolveResult:
    """Internal resolver result (not agent-facing)."""

    def __init__(
        self,
        *,
        decision: str,
        registry_contact_id: str | None = None,
        ghl_contact_id: str | None = None,
        location_id: str | None = None,
        lifecycle_state: str | None = None,
        cutoff_decision: str | None = None,
        masked_metadata: dict[str, Any] | None = None,
        message: str = "",
        match_outcome: str = MATCH_OUTCOME_UNAVAILABLE,
        candidate_rows: list[dict[str, Any]] | None = None,
        candidate_collision: bool = False,
        selected_contact_id: str | None = None,
    ) -> None:
        self.decision = decision
        self.registry_contact_id = registry_contact_id
        self.ghl_contact_id = ghl_contact_id
        self.location_id = location_id
        self.lifecycle_state = lifecycle_state
        self.cutoff_decision = cutoff_decision
        self.masked_metadata = masked_metadata or {}
        self.message = message
        # OPS-18 additive fields. Never influence `decision`.
        self.match_outcome = match_outcome
        self.candidate_rows = candidate_rows
        self.candidate_collision = candidate_collision
        # Set only when a `selection_token` was passed to resolve_event() and
        # it matched exactly one currently-registered candidate. Internal
        # use only (ingestion_runner); never included in any *Result.to_dict().
        self.selected_contact_id = selected_contact_id

    @property
    def authorized(self) -> bool:
        return self.decision == "allow"


# --- Registry ---

class ContactRegistry:
    """Thread-safe, persistently-backed Team Duncan contact activation registry.

    All state changes are atomic (temp-file + os.replace).  The HMAC key file
    is loaded lazily and cached; it is never emitted.

    Args:
        hermes_home: The HERMES_HOME directory.  Plugin data lives at
            ``<hermes_home>/plugin-data/team_duncan_contacts/``.
        team_duncan_location_id: The expected GHL location ID for Team Duncan.
            Contacts outside this location are rejected.
        clock: Injected UTC clock for testing.  Defaults to datetime.now(UTC).
    """

    def __init__(
        self,
        hermes_home: Path,
        *,
        team_duncan_location_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._hermes_home = Path(hermes_home)
        self._location_id = team_duncan_location_id
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._key_cache: bytes | None = None

    # --- Directory and file paths ---

    @property
    def _data_dir(self) -> Path:
        d = self._hermes_home / "plugin-data" / PLUGIN_DATA_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _key_path(self) -> Path:
        return self._data_dir / _KEY_FILE_NAME

    @property
    def _state_path(self) -> Path:
        return self._data_dir / _STATE_FILE_NAME

    # --- HMAC key management ---

    def _load_or_create_key(self, *, allow_create: bool) -> bytes:
        """Load the HMAC key from disk.

        If the key file does not exist and *allow_create* is True (only valid
        when no contacts have been registered yet), generates and saves a new key.
        Raises MissingHmacKeyError if the key is absent and creation is not allowed.
        """
        if self._key_cache is not None:
            return self._key_cache

        kp = self._key_path
        if kp.exists():
            key = kp.read_bytes()
            if len(key) != _KEY_LENGTH:
                raise CorruptStateError(
                    f"HMAC key file {kp} has unexpected length {len(key)} "
                    f"(expected {_KEY_LENGTH})."
                )
            self._key_cache = key
            return key

        if not allow_create:
            raise MissingHmacKeyError(
                f"HMAC key file {kp} is missing but activated contacts exist. "
                "Registry cannot operate without the key. Restore from backup."
            )

        key = secrets.token_bytes(_KEY_LENGTH)
        kp.write_bytes(key)
        os.chmod(kp, stat.S_IRUSR | stat.S_IWUSR)
        self._key_cache = key
        return key

    def _get_key(self) -> bytes:
        """Return the HMAC key, failing closed if absent when contacts exist."""
        state = self._load_state_raw()
        has_contacts = bool(state.get("contacts"))
        return self._load_or_create_key(allow_create=not has_contacts)

    # --- State persistence ---

    def _load_state_raw(self) -> dict[str, Any]:
        """Load the state file, returning an empty skeleton if absent."""
        sp = self._state_path
        if not sp.exists():
            return {"schema_version": SCHEMA_VERSION, "contacts": {}, "pending_tokens": {}}
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CorruptStateError(f"Cannot parse state file {sp}: {exc}") from exc
        return data

    def _save_state(self, state: dict[str, Any]) -> None:
        """Atomically write state to disk with restrictive permissions."""
        sp = self._state_path
        tmp = sp.with_suffix(".tmp")
        content = json.dumps(state, indent=2, default=str).encode("utf-8")
        try:
            tmp.write_bytes(content)
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            tmp_fd = os.open(str(tmp), os.O_RDONLY)
            try:
                os.fsync(tmp_fd)
            finally:
                os.close(tmp_fd)
            os.replace(tmp, sp)
            # Ensure 0600 on the final file (replace preserves the tmp chmod)
            os.chmod(sp, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # --- Schema/permission validation ---

    def startup_validate(self) -> None:
        """Validate state integrity, permissions, and HMAC key availability.

        Should be called once at plugin startup.  Raises on any integrity failure.
        """
        sp = self._state_path
        kp = self._key_path

        if sp.exists():
            mode = stat.S_IMODE(sp.stat().st_mode)
            if mode & ~(stat.S_IRUSR | stat.S_IWUSR):
                log.warning("State file %s has insecure permissions %04o", sp, mode)

        if kp.exists():
            mode = stat.S_IMODE(kp.stat().st_mode)
            if mode & ~(stat.S_IRUSR | stat.S_IWUSR):
                raise CorruptStateError(
                    f"HMAC key file {kp} has insecure permissions {mode:04o}. "
                    "Refusing to start."
                )

        state = self._load_state_raw()
        version = state.get("schema_version")
        if version is not None and version != SCHEMA_VERSION:
            raise CorruptStateError(
                f"Unsupported registry schema version {version!r}. "
                f"Expected {SCHEMA_VERSION}."
            )

        has_contacts = bool(state.get("contacts"))
        if has_contacts:
            if not kp.exists():
                raise MissingHmacKeyError(
                    f"Registry has contacts but HMAC key file {kp} is missing. "
                    "Refusing to start without the key."
                )
            self._load_or_create_key(allow_create=False)

    # --- Token management (internal) ---

    def _now_iso(self) -> str:
        return self._clock().isoformat()

    def _token_expired(self, token_data: dict[str, Any]) -> bool:
        try:
            exp = datetime.fromisoformat(token_data["expires_at"])
        except (KeyError, ValueError):
            return True
        return self._clock() > exp

    def _expire_tokens(self, state: dict[str, Any]) -> dict[str, Any]:
        """Remove expired pending tokens from state (mutates in-place, returns state)."""
        tokens = state.get("pending_tokens", {})
        expired = [t for t, d in tokens.items() if self._token_expired(d)]
        for t in expired:
            del tokens[t]
        return state

    # --- Public API ---

    def prepare_activation(
        self,
        name_or_id: str,
        ghl_reader: GhlContactReader,
    ) -> PrepareResult:
        """Resolve contact identity and produce a short-lived confirmation token.

        Steps:
        1. Validate the input (no phone/email/Apple handles accepted).
        2. Look up the contact via the injected GHL reader.
        3. Verify exactly one match within the Team Duncan location.
        4. Compute HMAC indexes and masked labels for all known handles.
        5. Generate a short-lived token containing non-PII token data.
        6. Return masked metadata + token (no raw handles).

        Zero, multiple, or non-Team-Duncan matches return review_required and
        write nothing to state.
        """
        try:
            query = validate_agent_name_or_id(name_or_id)
        except InvalidInputError as exc:
            return PrepareResult(
                status="invalid_input",
                reason="invalid_input",
                message=str(exc),
            )

        with self._lock:
            key = self._get_key()

            # Resolve contact: try both ID lookup and name search, then merge.
            # Always attempt ID lookup in case the input is an exact GHL ID.
            # Also do name search in case the input is a display name.
            id_contact = ghl_reader.get_contact_by_id(query)
            id_candidates = [id_contact] if id_contact else []

            # Only search by name if the input looks like a name (not a pure ID).
            # IDs are typically long alphanumeric strings with no spaces;
            # names usually contain letters and may have spaces.
            name_candidates = ghl_reader.search_contacts_by_name(
                query, self._location_id
            )

            # Merge, deduplicate by contact ID
            seen_ids: set[str] = set()
            candidates: list[dict] = []
            for c in id_candidates + name_candidates:
                if c and c.get("id") not in seen_ids:
                    seen_ids.add(c["id"])
                    candidates.append(c)

            # Filter to Team Duncan location
            td_candidates = [
                c for c in candidates
                if c and c.get("locationId") == self._location_id
            ]

            if len(td_candidates) == 0:
                if not candidates:
                    # No GHL contact found at all: must be created in GHL first.
                    # Prerequisite: OPS-16 (GHL contact creation workflow).
                    return PrepareResult(
                        status="contact_creation_required",
                        reason="no_ghl_contact",
                        message=(
                            "No GoHighLevel contact exists for this identity. "
                            "The contact must be created in GHL before activation "
                            "can proceed. Complete OPS-16 (GHL contact creation) "
                            "first, then retry. No state was written."
                        ),
                    )
                # A GHL contact was found but it belongs to a different location.
                return PrepareResult(
                    status="review_required",
                    reason="wrong_location",
                    message=(
                        "A GoHighLevel contact was found but does not belong to "
                        "the Team Duncan location. Verify the contact's location "
                        "or provide the exact Team Duncan GoHighLevel contact ID. "
                        "No state was written."
                    ),
                )

            if len(td_candidates) > 1:
                return PrepareResult(
                    status="review_required",
                    reason="multiple_matches",
                    message=(
                        f"Found {len(td_candidates)} contacts matching that query "
                        "within the Team Duncan account. Provide the exact "
                        "GoHighLevel contact ID to disambiguate. No state was written."
                    ),
                )

            contact = td_candidates[0]
            contact_id = contact["id"]
            location_id = contact["locationId"]
            display_name = contact_display_name(contact)

            # Compute HMAC indexes and masked labels from raw handles
            raw_handles = extract_handle_kinds(contact)
            hmac_indexes: dict[str, str] = {}
            masked_handles: dict[str, str] = {}
            for kind, raw in raw_handles.items():
                canonical = _canonicalize_handle(raw, kind)
                hmac_indexes[kind] = _hmac_hex(key, canonical)
                masked_handles[kind] = _mask_handle(raw, kind)

            # Candidate-label fields (OPS-18): derived once here from the same
            # contact dict already returned by the reader, before raw handles
            # go out of scope. Carried through the token into the activation
            # record for later multiple_match candidate rendering.
            candidate_label_fields = _extract_candidate_label_fields(contact)

            # Issue token
            token = secrets.token_urlsafe(32)
            now = self._clock()
            from datetime import timedelta as _td
            expires_iso = (now + _td(seconds=_TOKEN_TTL_SECONDS)).isoformat()

            state = self._load_state_raw()
            self._expire_tokens(state)

            state.setdefault("pending_tokens", {})[token] = {
                "contact_id": contact_id,
                "contact_name": display_name,
                "location_id": location_id,
                "masked_handles": masked_handles,
                "hmac_indexes": hmac_indexes,
                "candidate_label_fields": candidate_label_fields,
                "expires_at": expires_iso,
            }
            self._save_state(state)

            return PrepareResult(
                status="ready_for_confirmation",
                contact_name=display_name,
                contact_id=contact_id,
                location_id=location_id,
                masked_handles=masked_handles,
                token=token,
                token_expires_at=expires_iso,
                message=(
                    "Identity resolved. Activation begins when you confirm with the "
                    "token above. Activity before the confirmation timestamp remains "
                    "unavailable through the registry."
                ),
            )

    def confirm_activation(self, token: str) -> ConfirmResult:
        """Confirm a pending activation and create the immutable activation record.

        The activation timestamp is generated inside this transaction using the
        injected clock.  The preparation timestamp is never used as the cutoff.

        Idempotent: confirming an already-activated contact returns the original
        activation data without moving the cutoff.

        Replaying an expired or missing token returns an error result.
        """
        token = (token or "").strip()
        if not token:
            return ConfirmResult(
                status="error",
                message="Token must not be empty.",
            )

        with self._lock:
            state = self._load_state_raw()
            self._expire_tokens(state)

            token_data = state.get("pending_tokens", {}).get(token)
            if token_data is None:
                return ConfirmResult(
                    status="error",
                    reason="token_not_found_or_expired",
                    message=(
                        "Confirmation token not found or expired. "
                        "Call prepare_activation again to get a new token."
                    ),
                )

            contact_id = token_data["contact_id"]
            display_name = token_data["contact_name"]
            location_id = token_data["location_id"]
            masked_handles = token_data["masked_handles"]
            hmac_indexes = token_data["hmac_indexes"]
            candidate_label_fields = token_data.get("candidate_label_fields") or {}

            # Idempotency: already activated
            existing = state.get("contacts", {}).get(contact_id)
            if existing:
                # Remove the token but do NOT move the cutoff
                state["pending_tokens"].pop(token, None)
                self._save_state(state)
                return ConfirmResult(
                    status="already_activated",
                    contact_name=existing["display_name"],
                    contact_id=contact_id,
                    location_id=existing["location_id"],
                    activated_at=existing["activated_at"],
                    masked_handles=existing.get("masked_labels", {}),
                    actor=existing.get("actor"),
                    message=(
                        "This contact was already activated. "
                        "The original activation timestamp is preserved."
                    ),
                )

            # Generate the immutable activation timestamp NOW
            activated_at = self._clock()
            activated_at_iso = activated_at.isoformat()

            activation_record = {
                "contact_id": contact_id,
                "location_id": location_id,
                "display_name": display_name,
                "state": "active",
                "activated_at": activated_at_iso,
                "actor": "clay",
                "approved_handle_kinds": list(hmac_indexes.keys()),
                "hmac_indexes": hmac_indexes,
                "masked_labels": masked_handles,
                "candidate_label_fields": candidate_label_fields,
                "history": [
                    {
                        "timestamp": activated_at_iso,
                        "transition": "activated",
                        "actor": "clay",
                    }
                ],
            }

            state.setdefault("contacts", {})[contact_id] = activation_record
            # Consume the token
            state["pending_tokens"].pop(token, None)
            self._save_state(state)

            return ConfirmResult(
                status="activated",
                contact_name=display_name,
                contact_id=contact_id,
                location_id=location_id,
                activated_at=activated_at_iso,
                masked_handles=masked_handles,
                actor="clay",
                message=(
                    "Contact activated. Tracking begins at the confirmation "
                    "timestamp above. Activity before that timestamp remains "
                    "unavailable through the registry."
                ),
            )

    def resolve_event(
        self,
        raw_handle: str,
        event_ts: datetime,
        *,
        source: str | None = None,
        source_event_id: str | None = None,
        selection_token: str | None = None,
    ) -> ResolveResult:
        """Internal resolver: decide whether a source event is authorized.

        Input: raw handle (phone/email/etc.) + event timestamp.
        Output: ResolveResult with decision, match_outcome, and masked metadata.

        This method writes no state and performs no GHL mutations.
        Raw handles are canonicalized and HMAC'd inside this method and never
        returned, logged, or stored externally.

        `source`/`source_event_id` are optional and additive (OPS-18): when
        both are given and the match_outcome is multiple_match, they are used
        to derive per-candidate opaque selection tokens. Existing callers that
        omit them (e.g. ActivityLedger.record_event) are unaffected; they get
        candidate_rows with no usable selection token, which they never use.

        `selection_token` is also optional and additive: when given, it is
        matched (constant-time) against every fresh candidate's own token,
        entirely inside this method, and the winning contact_id (if any) is
        returned via `ResolveResult.selected_contact_id`. Candidate rows
        themselves never carry a contact_id (non-PII by design), so this is
        the only way to turn Clay's exact token back into a contact.
        """
        with self._lock:
            state = self._load_state_raw()
            contacts = state.get("contacts", {})

            if not contacts:
                return ResolveResult(
                    decision="review_required",
                    match_outcome=MATCH_OUTCOME_UNAVAILABLE,
                    message="No activated contacts in registry.",
                )

            try:
                key = self._load_or_create_key(allow_create=False)
            except MissingHmacKeyError:
                return ResolveResult(
                    decision="review_required",
                    match_outcome=MATCH_OUTCOME_UNAVAILABLE,
                    message="HMAC key unavailable; cannot resolve handle.",
                )

            # Determine handle kind and canonicalize
            digit_count = sum(c.isdigit() for c in raw_handle)
            kind = "phone" if digit_count >= 10 else "email" if "@" in raw_handle else "other"
            canonical = _canonicalize_handle(raw_handle, kind)
            input_hmac = _hmac_hex(key, canonical)

            matches: list[str] = []
            for cid, contact in contacts.items():
                for stored_hmac in contact.get("hmac_indexes", {}).values():
                    if secrets.compare_digest(stored_hmac, input_hmac):
                        matches.append(cid)
                        break

            if len(matches) == 0:
                return ResolveResult(
                    decision="review_required",
                    match_outcome=MATCH_OUTCOME_ZERO,
                    message=(
                        "No registry match for the provided handle. "
                        "No event is authorized."
                    ),
                )

            if len(matches) > 1:
                return self._resolve_multiple_match(
                    key, contacts, matches, source, source_event_id, selection_token
                )

            contact_id = matches[0]
            contact = contacts[contact_id]
            activated_at = datetime.fromisoformat(contact["activated_at"])
            lifecycle = contact["state"]

            masked_meta = {
                "contact_id": contact_id,
                "location_id": contact.get("location_id"),
                "display_name": contact.get("display_name"),
                "masked_labels": contact.get("masked_labels", {}),
                "lifecycle_state": lifecycle,
            }

            # Timestamp decision
            if event_ts < activated_at:
                cutoff_decision = "before_cutoff"
                return ResolveResult(
                    decision="deny_pre_activation",
                    ghl_contact_id=contact_id,
                    location_id=contact.get("location_id"),
                    lifecycle_state=lifecycle,
                    cutoff_decision=cutoff_decision,
                    masked_metadata=masked_meta,
                    match_outcome=MATCH_OUTCOME_UNIQUE,
                    message=(
                        "Event timestamp is before the contact's activation cutoff. "
                        "No write or visibility is authorized."
                    ),
                )

            cutoff_decision = "at_cutoff" if event_ts == activated_at else "after_cutoff"

            # Lifecycle decision
            if lifecycle == "paused":
                return ResolveResult(
                    decision="deny_paused",
                    ghl_contact_id=contact_id,
                    location_id=contact.get("location_id"),
                    lifecycle_state=lifecycle,
                    cutoff_decision=cutoff_decision,
                    masked_metadata=masked_meta,
                    match_outcome=MATCH_OUTCOME_UNIQUE,
                    message="Contact is paused; new events are not authorized.",
                )

            if lifecycle == "retired":
                return ResolveResult(
                    decision="deny_retired",
                    ghl_contact_id=contact_id,
                    location_id=contact.get("location_id"),
                    lifecycle_state=lifecycle,
                    cutoff_decision=cutoff_decision,
                    masked_metadata=masked_meta,
                    match_outcome=MATCH_OUTCOME_UNIQUE,
                    message="Contact is retired; new events are not authorized.",
                )

            return ResolveResult(
                decision="allow",
                registry_contact_id=contact_id,
                ghl_contact_id=contact_id,
                location_id=contact.get("location_id"),
                lifecycle_state=lifecycle,
                cutoff_decision=cutoff_decision,
                masked_metadata=masked_meta,
                match_outcome=MATCH_OUTCOME_UNIQUE,
                message="Event authorized.",
            )

    def _resolve_multiple_match(
        self,
        key: bytes,
        contacts: dict[str, Any],
        matches: list[str],
        source: str | None,
        source_event_id: str | None,
        selection_token: str | None = None,
    ) -> ResolveResult:
        """Build (and collision-check) candidate rows for a multiple_match event.

        Fails closed: any two candidates rendering an identical display_label
        withhold every candidate row and report candidate_collision=True. No
        GHL read happens here; labels are derived only from what the registry
        already stored at prepare/confirm time.
        """
        labels: dict[str, str] = {
            cid: build_candidate_display_label(contacts[cid]) for cid in matches
        }
        collision = len(set(labels.values())) != len(labels)

        if collision:
            return ResolveResult(
                decision="review_required",
                match_outcome=MATCH_OUTCOME_MULTIPLE,
                candidate_rows=None,
                candidate_collision=True,
                message=(
                    "Multiple registry contacts matched and at least two "
                    "render an identical candidate label. No candidate can be "
                    "safely selected; resolve directly in GHL."
                ),
            )

        candidate_rows = []
        selected_contact_id = None
        for cid in matches:
            token = None
            if source and source_event_id:
                token = _selection_token(key, cid, source, source_event_id)
                if (
                    selection_token is not None
                    and secrets.compare_digest(token, selection_token)
                ):
                    selected_contact_id = cid
            candidate_rows.append(
                {"selection_token": token, "display_label": labels[cid]}
            )

        return ResolveResult(
            decision="review_required",
            match_outcome=MATCH_OUTCOME_MULTIPLE,
            candidate_rows=candidate_rows,
            candidate_collision=False,
            selected_contact_id=selected_contact_id,
            message=(
                "Multiple registry contacts matched the provided handle. "
                "Exact opaque selection is required; no event is authorized "
                "without it."
            ),
        )

    # --- Lifecycle management ---

    def _transition(
        self,
        contact_id: str,
        new_state: str,
        actor: str,
        reason: str,
    ) -> None:
        """Internal: append an immutable transition and update lifecycle state."""
        with self._lock:
            state = self._load_state_raw()
            contact = state.get("contacts", {}).get(contact_id)
            if contact is None:
                raise RegistryError(f"Contact {contact_id!r} not found in registry.")

            now_iso = self._now_iso()
            contact["history"].append(
                {
                    "timestamp": now_iso,
                    "transition": new_state,
                    "actor": actor,
                    "reason": reason,
                }
            )
            contact["state"] = new_state
            self._save_state(state)

    def pause_contact(self, contact_id: str, actor: str, reason: str = "") -> None:
        """Pause an active contact.  Preserves history and original activated_at."""
        self._transition(contact_id, "paused", actor, reason)

    def resume_contact(self, contact_id: str, actor: str, reason: str = "") -> None:
        """Resume a paused contact.  Preserves original activated_at."""
        with self._lock:
            state = self._load_state_raw()
            contact = state.get("contacts", {}).get(contact_id)
            if contact is None:
                raise RegistryError(f"Contact {contact_id!r} not found.")
            if contact["state"] == "retired":
                raise RetiredContactError(
                    f"Contact {contact_id!r} is retired and cannot be reactivated "
                    "in this build."
                )
        self._transition(contact_id, "active", actor, reason)

    def retire_contact(self, contact_id: str, actor: str, reason: str = "") -> None:
        """Retire a contact permanently.  Not reversible in this build."""
        self._transition(contact_id, "retired", actor, reason)

    # --- Export / restore ---

    def export_state(self) -> dict[str, Any]:
        """Return the registry state for backup.

        Contains HMAC indexes, activation timestamps, and lifecycle history.
        Does not contain raw handles.
        """
        with self._lock:
            state = self._load_state_raw()
            # Exclude pending tokens from export (they are short-lived)
            export = dict(state)
            export.pop("pending_tokens", None)
            return export

    def restore_state(self, data: dict[str, Any]) -> None:
        """Restore registry state from an export dict.

        Preserves HMAC indexes and original activation timestamps.
        """
        with self._lock:
            if data.get("schema_version") != SCHEMA_VERSION:
                raise CorruptStateError(
                    f"Cannot restore: schema version {data.get('schema_version')!r} "
                    f"does not match current version {SCHEMA_VERSION}."
                )
            state = {
                "schema_version": SCHEMA_VERSION,
                "contacts": data.get("contacts", {}),
                "pending_tokens": {},
            }
            self._save_state(state)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
