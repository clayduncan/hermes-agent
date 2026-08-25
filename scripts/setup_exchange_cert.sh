#!/usr/bin/env bash
# setup_exchange_cert.sh — Local certificate setup for Exchange Online app-only auth.
#
# Generates a self-signed RSA certificate, exports a password-protected PFX for
# unattended use, exports the public certificate for Entra upload, and stores the
# PFX password in macOS Keychain. Operates only on a caller-specified target
# directory outside the repository.
#
# Usage:
#   ./setup_exchange_cert.sh --target-dir /path/to/certs [OPTIONS]
#
# Flags:
#   --target-dir DIR        Required. Directory to write credential files into.
#   --apply                 Actually create files and Keychain entry. Default: dry run.
#   --replace               Allow overwriting existing certificate artifacts.
#   --common-name NAME      Certificate CN (default: ExchangeAppOnly).
#   --days N                Certificate validity in days (default: 365).
#   --keychain-service SVC  Keychain service name (default: hermes-exchange-pfx).
#   --keychain-account ACT  Keychain account name (default: exchange-app-only).

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────

TARGET_DIR=""
APPLY=0
REPLACE=0
COMMON_NAME="ExchangeAppOnly"
DAYS=365
KEYCHAIN_SERVICE="hermes-exchange-pfx"
KEYCHAIN_ACCOUNT="exchange-app-only"

OPENSSL="${OPENSSL:-/usr/bin/openssl}"
SECURITY="${SECURITY:-/usr/bin/security}"

# ── Argument parsing ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-dir)     TARGET_DIR="$2";        shift 2 ;;
        --apply)          APPLY=1;                shift   ;;
        --replace)        REPLACE=1;              shift   ;;
        --common-name)    COMMON_NAME="$2";       shift 2 ;;
        --days)           DAYS="$2";              shift 2 ;;
        --keychain-service) KEYCHAIN_SERVICE="$2"; shift 2 ;;
        --keychain-account) KEYCHAIN_ACCOUNT="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Validation ─────────────────────────────────────────────────────────────────

if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: --target-dir is required." >&2
    exit 1
fi

if [[ ! -x "$OPENSSL" ]]; then
    echo "Error: openssl not found at $OPENSSL." >&2
    exit 1
fi

if [[ ! -x "$SECURITY" ]]; then
    echo "Error: security not found at $SECURITY." >&2
    exit 1
fi

if [[ "$DAYS" -lt 1 ]] 2>/dev/null; then
    echo "Error: --days must be a positive integer." >&2
    exit 1
fi

if [[ -z "$COMMON_NAME" || -z "$KEYCHAIN_SERVICE" || -z "$KEYCHAIN_ACCOUNT" ]]; then
    echo "Error: --common-name, --keychain-service, and --keychain-account must not be empty." >&2
    exit 1
fi

# Resolve to absolute path; the parent directory need not yet exist.
_td_parent="$(dirname "$TARGET_DIR")"
if [[ -d "$_td_parent" ]]; then
    _td_parent="$(cd "$_td_parent" && pwd)"
elif [[ "$_td_parent" != /* ]]; then
    _td_parent="$(pwd)/$_td_parent"
fi
TARGET_DIR="$_td_parent/$(basename "$TARGET_DIR")"
unset _td_parent

# Reject if TARGET_DIR appears to be inside the repository.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$TARGET_DIR" == "$REPO_ROOT"* ]]; then
    echo "Error: --target-dir must be outside the repository ($REPO_ROOT)." >&2
    exit 1
fi

# Artifact paths.
CERT_PEM="$TARGET_DIR/exchange_app.crt"
PFX_FILE="$TARGET_DIR/exchange_app.pfx"
PUB_CERT="$TARGET_DIR/exchange_app_public.crt"

# ── Dry-run output ─────────────────────────────────────────────────────────────

echo "Plan:"
echo "  Target directory : $TARGET_DIR"
echo "  Certificate CN   : $COMMON_NAME"
echo "  Validity         : ${DAYS} days"
echo "  Keychain service : $KEYCHAIN_SERVICE"
echo "  Keychain account : $KEYCHAIN_ACCOUNT"
echo "  PFX output       : $PFX_FILE"
echo "  Public cert      : $PUB_CERT"
echo "  Overwrite mode   : $([ "$REPLACE" -eq 1 ] && echo enabled || echo disabled)"
echo ""

if [[ "$APPLY" -eq 0 ]]; then
    echo "Dry run — pass --apply to create artifacts."
    exit 0
fi

# ── Apply ──────────────────────────────────────────────────────────────────────

# Guard against overwriting existing artifacts without --replace.
for ARTIFACT in "$CERT_PEM" "$PFX_FILE" "$PUB_CERT"; do
    if [[ -e "$ARTIFACT" && "$REPLACE" -eq 0 ]]; then
        echo "Error: artifact already exists: $ARTIFACT" >&2
        echo "Pass --replace to allow overwriting." >&2
        exit 1
    fi
done

# Check for existing Keychain item without --replace.
if [[ "$REPLACE" -eq 0 ]]; then
    if "$SECURITY" find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" >/dev/null 2>&1; then
        echo "Error: Keychain item already exists (service=$KEYCHAIN_SERVICE, account=$KEYCHAIN_ACCOUNT)." >&2
        echo "Pass --replace to allow overwriting." >&2
        exit 1
    fi
fi

mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"

# Generate strong random password (never printed).
PFX_PASSWORD="$("$OPENSSL" rand -base64 48)"

# Temp private key — cleaned up in all exit paths.
TMP_KEY="$TARGET_DIR/.exchange_app_key_tmp.pem"
cleanup() {
    if [[ -f "$TMP_KEY" ]]; then
        "$OPENSSL" rand -out "$TMP_KEY" 4096 2>/dev/null || true
        rm -f "$TMP_KEY"
    fi
}
trap cleanup EXIT

# Generate RSA private key (4096-bit) into temp file.
"$OPENSSL" genrsa -out "$TMP_KEY" 4096 2>/dev/null
chmod 600 "$TMP_KEY"

# Generate self-signed certificate.
"$OPENSSL" req -new -x509 \
    -key "$TMP_KEY" \
    -out "$CERT_PEM" \
    -days "$DAYS" \
    -subj "/CN=${COMMON_NAME}" \
    2>/dev/null
chmod 600 "$CERT_PEM"

# Export public certificate (PEM format for Entra upload).
"$OPENSSL" x509 -in "$CERT_PEM" -out "$PUB_CERT" -outform PEM
chmod 644 "$PUB_CERT"

# Export password-protected PFX. Password passed via environment to avoid
# appearing in the process list.
PFXPASSWORD="$PFX_PASSWORD" "$OPENSSL" pkcs12 -export \
    -in "$CERT_PEM" \
    -inkey "$TMP_KEY" \
    -out "$PFX_FILE" \
    -passout env:PFXPASSWORD \
    2>/dev/null
chmod 600 "$PFX_FILE"

# Store PFX password in macOS Keychain.
# -U updates if the item already exists (needed when --replace is set).
KEYCHAIN_OK=0
if "$SECURITY" add-generic-password \
        -s "$KEYCHAIN_SERVICE" \
        -a "$KEYCHAIN_ACCOUNT" \
        -w "$PFX_PASSWORD" \
        -U 2>/dev/null; then
    KEYCHAIN_OK=1
fi

# Clear password from memory as soon as possible.
PFX_PASSWORD=""
unset PFX_PASSWORD

# Emit safe metadata only.
THUMBPRINT="$("$OPENSSL" x509 -in "$CERT_PEM" -noout -fingerprint -sha1 2>/dev/null | cut -d= -f2)"
EXPIRY="$("$OPENSSL" x509 -in "$CERT_PEM" -noout -enddate 2>/dev/null | cut -d= -f2)"

echo "Certificate created:"
echo "  PFX              : $PFX_FILE"
echo "  Public cert      : $PUB_CERT"
echo "  Thumbprint       : $THUMBPRINT"
echo "  Expires          : $EXPIRY"
echo "  Keychain stored  : $([ "$KEYCHAIN_OK" -eq 1 ] && echo yes || echo FAILED)"

if [[ "$KEYCHAIN_OK" -eq 0 ]]; then
    echo "Warning: Keychain storage failed. Remove the PFX and retry." >&2
    exit 1
fi
