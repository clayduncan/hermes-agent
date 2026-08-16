#!/usr/bin/env python3
"""Microsoft Graph delegated (user-consent) OAuth2 setup for Hermes Agent.

Fully non-interactive — designed to be driven by the agent via terminal commands.
The agent mediates between this script and the user (works over CLI, Telegram,
Discord, etc.).  The user never interacts with this script directly.

Commands:
  setup.py --check --account KEY
  setup.py --configure --account KEY --tenant-id ID --client-id ID [--scopes "s1 s2"]
  setup.py --auth-url --account KEY
  setup.py --auth-code "CODE_OR_FULL_REDIRECT_URL" --account KEY
  setup.py --revoke --account KEY

Agent workflow:
  1. Run --check --account KEY.  Exit 0 = auth good, skip setup.
  2. Run --configure --account KEY --tenant-id TENANT_ID --client-id CLIENT_ID.
     The client secret must already be set in env as MSGRAPH_<KEY>_CLIENT_SECRET.
  3. Run --auth-url --account KEY.  Send the printed URL to the user.
  4. User opens URL in browser, approves consent.  The browser is redirected to
     https://login.microsoftonline.com/common/oauth2/nativeclient?code=...
     which shows an error page — that is expected.  The user copies either:
       a. Just the 'code' query parameter value from the URL, OR
       b. The entire redirect URL from the browser address bar.
  5. User pastes that.  Agent runs --auth-code "CODE_OR_URL" --account KEY.
  6. Run --check --account KEY to verify.  Done.

Env var naming:
  MSGRAPH_<KEY>_TENANT_ID     — Azure Directory (tenant) ID
  MSGRAPH_<KEY>_CLIENT_ID     — Azure Application (client) ID
  MSGRAPH_<KEY>_CLIENT_SECRET — Client secret (never stored on disk)

Example for the 'clayduncan' account:
  MSGRAPH_CLAYDUNCAN_TENANT_ID=d14d6257-4cf6-45a2-83fd-5d218bda8aee
  MSGRAPH_CLAYDUNCAN_CLIENT_ID=1bb196a6-47c2-40d0-be2a-f38ddb007820
  MSGRAPH_CLAYDUNCAN_CLIENT_SECRET=<secret>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Add the repo root to sys.path so we can import from tools/ when run standalone.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.microsoft_graph_delegated_auth import (
    DEFAULT_GRAPH_AUTHORITY_URL,
    DEFAULT_REDIRECT_URI,
    DelegatedGraphReconsentNeeded,
    DelegatedGraphSetupNeeded,
    DelegatedTokenFile,
    DelegatedTokenProvider,
    _get_hermes_home,
)

# Default delegated scopes for new accounts.  offline_access is required to get
# a refresh_token; the module adds it automatically, but it's listed here too so
# users can see it in the pending-session file.
DEFAULT_SCOPES = [
    "Calendars.ReadWrite",
    "Mail.ReadWrite",
    "offline_access",
]


# ── Path helpers ──────────────────────────────────────────────────────────────


def _config_path(account_key: str) -> Path:
    return _get_hermes_home() / f"msgraph_config_{account_key}.json"


def _pending_path(account_key: str) -> Path:
    return _get_hermes_home() / f"msgraph_pending_{account_key}.json"


def _token_path(account_key: str) -> Path:
    return _get_hermes_home() / f"msgraph_token_{account_key}.json"


# ── Config file (tenant_id + client_id + scopes, no secrets) ─────────────────


def _load_config(account_key: str) -> dict:
    path = _config_path(account_key)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(
    account_key: str,
    *,
    tenant_id: str,
    client_id: str,
    scopes: list[str],
) -> None:
    path = _config_path(account_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"tenant_id": tenant_id, "client_id": client_id, "scopes": scopes},
            indent=2,
        ),
        encoding="utf-8",
    )


# ── PKCE helpers ──────────────────────────────────────────────────────────────


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    challenge_bytes = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ── Pending OAuth session ─────────────────────────────────────────────────────


def _save_pending_session(
    account_key: str,
    *,
    state: str,
    code_verifier: str,
    redirect_uri: str,
    tenant_id: str,
    client_id: str,
    scopes: list[str],
) -> None:
    path = _pending_path(account_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "scopes": scopes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_pending_session(account_key: str) -> dict:
    path = _pending_path(account_key)
    if not path.exists():
        print(f"ERROR: No pending OAuth session for account '{account_key}'. Run --auth-url first.")
        sys.exit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: Could not read pending OAuth session: {exc}")
        print("Run --auth-url again to start a fresh session.")
        sys.exit(1)
    for required_key in ("state", "code_verifier", "redirect_uri", "tenant_id", "client_id"):
        if not data.get(required_key):
            print(f"ERROR: Pending session is missing '{required_key}'. Run --auth-url again.")
            sys.exit(1)
    return data


# ── Code extraction from raw code or full redirect URL ───────────────────────


def _extract_code_and_state(code_or_url: str) -> tuple[str, str | None]:
    """Accept a raw auth code or the full redirect URL pasted by the user."""
    if not code_or_url.startswith("http"):
        return code_or_url.strip(), None
    parsed = urllib.parse.urlparse(code_or_url)
    params = urllib.parse.parse_qs(parsed.query)
    if "error" in params:
        error = (params.get("error") or ["unknown"])[0]
        desc = (params.get("error_description") or [""])[0]
        print(f"ERROR: Authorization server returned error '{error}': {desc}")
        sys.exit(1)
    if "code" not in params:
        print("ERROR: No 'code' parameter found in redirect URL.")
        sys.exit(1)
    state = (params.get("state") or [None])[0]
    return params["code"][0], state


# ── Client secret resolution ──────────────────────────────────────────────────


def _get_client_secret(account_key: str) -> str:
    key = f"MSGRAPH_{account_key.upper()}_CLIENT_SECRET"
    secret = (os.environ.get(key) or "").strip()
    if not secret:
        print(f"ERROR: {key} is not set in the environment.")
        print(f"Add it to ~/.hermes/.env and restart the gateway.")
        sys.exit(1)
    return secret


# ── Commands ──────────────────────────────────────────────────────────────────


def check_auth(account_key: str) -> bool:
    """Check whether stored credentials are valid.  Prints status, returns bool."""
    provider = DelegatedTokenProvider(account_key=account_key, hermes_home=_get_hermes_home())
    path = provider.token_path

    if not path.exists():
        print(f"NOT_AUTHENTICATED: No token file for account '{account_key}' at {path}")
        return False

    try:
        token_file = DelegatedTokenFile.load(path)
    except Exception as exc:
        print(f"TOKEN_CORRUPT: {exc}")
        return False

    if not token_file.is_expired():
        print(
            f"AUTHENTICATED: Token valid for account '{account_key}' at {path}\n"
            f"  Expires in: {token_file.expires_in_seconds}s\n"
            f"  Scopes: {', '.join(token_file.scopes)}"
        )
        return True

    if token_file.refresh_token:
        env_key = f"MSGRAPH_{account_key.upper()}_CLIENT_SECRET"
        if not (os.environ.get(env_key) or "").strip():
            print(
                f"TOKEN_EXPIRED: Token for account '{account_key}' is expired and "
                f"{env_key} is not set — cannot auto-refresh."
            )
            return False
        print(f"TOKEN_EXPIRED: Token for account '{account_key}' is expired — "
              "has refresh_token, will auto-refresh on next use.")
        return True

    print(f"TOKEN_INVALID: Token for account '{account_key}' is expired with no refresh_token.")
    return False


def configure(
    account_key: str,
    *,
    tenant_id: str,
    client_id: str,
    scopes: list[str] | None = None,
) -> None:
    """Store tenant_id, client_id, and scopes for an account (no secret stored)."""
    # Validate that the client secret env var exists right now, so the user
    # gets a clear error here rather than mysteriously later.
    _get_client_secret(account_key)

    resolved_scopes = scopes or DEFAULT_SCOPES
    if "offline_access" not in resolved_scopes:
        resolved_scopes = [*resolved_scopes, "offline_access"]

    _save_config(
        account_key,
        tenant_id=tenant_id.strip(),
        client_id=client_id.strip(),
        scopes=resolved_scopes,
    )
    path = _config_path(account_key)
    print(f"OK: Configuration saved for account '{account_key}' at {path}")
    print(f"  Tenant ID : {tenant_id.strip()}")
    print(f"  Client ID : {client_id.strip()}")
    print(f"  Scopes    : {', '.join(resolved_scopes)}")
    print(f"\nNext step: run --auth-url --account {account_key}")


def get_auth_url(account_key: str) -> None:
    """Build and print the authorization URL.  The user visits this in a browser."""
    config = _load_config(account_key)
    if not config.get("tenant_id") or not config.get("client_id"):
        print(
            f"ERROR: No configuration for account '{account_key}'. "
            f"Run --configure --account {account_key} --tenant-id ID --client-id ID first."
        )
        sys.exit(1)

    tenant_id: str = config["tenant_id"]
    client_id: str = config["client_id"]
    scopes: list[str] = config.get("scopes") or DEFAULT_SCOPES

    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    authorize_url = (
        f"{DEFAULT_GRAPH_AUTHORITY_URL}/{tenant_id}/oauth2/v2.0/authorize"
    )
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",  # Force consent screen so refresh_token is always issued
        "access_type": "offline",
    }
    full_url = f"{authorize_url}?{urllib.parse.urlencode(params)}"

    _save_pending_session(
        account_key,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=DEFAULT_REDIRECT_URI,
        tenant_id=tenant_id,
        client_id=client_id,
        scopes=scopes,
    )

    print(full_url)
    print(
        f"\n[Agent note] Send this URL to the user. After they approve consent, the browser "
        f"will redirect to a page that shows an error — that is expected. Ask the user to "
        f"copy the entire URL from their browser address bar (or just the 'code=...' value) "
        f"and paste it back. Then run:\n"
        f"  python scripts/microsoft_graph_delegated_setup.py "
        f'--auth-code "PASTED_CODE_OR_URL" --account {account_key}',
        file=sys.stderr,
    )


def exchange_auth_code(account_key: str, code_or_url: str) -> None:
    """Exchange the authorization code for tokens and save them."""
    pending = _load_pending_session(account_key)
    code, returned_state = _extract_code_and_state(code_or_url)

    if returned_state and returned_state != pending["state"]:
        print(
            "ERROR: OAuth state mismatch — possible CSRF or stale session. "
            "Run --auth-url again to start a fresh session."
        )
        sys.exit(1)

    client_secret = _get_client_secret(account_key)
    tenant_id: str = pending["tenant_id"]
    client_id: str = pending["client_id"]
    scopes: list[str] = pending.get("scopes") or DEFAULT_SCOPES

    token_url = f"{DEFAULT_GRAPH_AUTHORITY_URL}/{tenant_id}/oauth2/v2.0/token"
    post_data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
            "scope": " ".join(scopes),
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            token_url,
            data=post_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("error_description") or body.get("error") or str(exc)
        except Exception:
            detail = str(exc)
        print(f"ERROR: Token exchange failed: {detail}")
        print("The code may have expired or already been used. Run --auth-url to get a fresh URL.")
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Token exchange failed: {exc}")
        sys.exit(1)

    import time as _time

    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 3600)
    scope_str = str(payload.get("scope") or "").strip()
    granted_scopes = scope_str.split() if scope_str else scopes

    if not access_token:
        print(f"ERROR: Token exchange succeeded but response has no access_token: {payload}")
        sys.exit(1)
    if not refresh_token:
        print(
            "WARNING: No refresh_token in response. "
            "offline_access scope may have been denied or the token was issued without consent prompt."
        )

    token_file = DelegatedTokenFile(
        account_key=account_key,
        tenant_id=tenant_id,
        client_id=client_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=_time.time() + max(0, expires_in),
        scopes=granted_scopes,
    )

    dest = _token_path(account_key)
    token_file.save(dest)

    # Clean up the pending session
    _pending_path(account_key).unlink(missing_ok=True)

    print(f"OK: Authenticated. Token saved to {dest}")
    print(f"  Account   : {account_key}")
    print(f"  Tenant ID : {tenant_id}")
    print(f"  Scopes    : {', '.join(granted_scopes)}")
    print(f"\nVerify with: python scripts/microsoft_graph_delegated_setup.py --check --account {account_key}")


def revoke(account_key: str) -> None:
    """Delete the local token file for this account."""
    token = _token_path(account_key)
    pending = _pending_path(account_key)
    config = _config_path(account_key)

    deleted: list[str] = []
    for path in (token, pending):
        if path.exists():
            path.unlink()
            deleted.append(str(path))

    if deleted:
        print(f"Deleted local files: {', '.join(deleted)}")
    else:
        print(f"No token or pending session found for account '{account_key}' — nothing to delete.")

    print(
        "\nIMPORTANT: This only removed the LOCAL token file.  The OAuth consent grant "
        "on Microsoft's side is still active.\n"
        "To fully revoke access on Microsoft's side, the user must:\n"
        "  1. Visit https://myaccount.microsoft.com/permissions\n"
        "  2. Find the app in 'Apps & services that have access to your data'\n"
        "  3. Click 'Remove access'\n"
        "Or, if this is an organization-managed account, the tenant admin can revoke it "
        "via the Azure portal (Entra ID → Enterprise applications → User consent).\n"
        "Until one of those steps is done, the app retains its granted permissions."
    )
    if config.exists():
        print(f"\nConfiguration file kept at {config} — run --configure to reconfigure.")


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Microsoft Graph delegated OAuth2 setup for Hermes Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--account",
        required=True,
        metavar="KEY",
        help="Account key (e.g. clayduncan, princeton)",
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Check if auth is valid (exit 0=yes, 1=no)",
    )
    mode_group.add_argument(
        "--configure",
        action="store_true",
        help="Store app registration IDs for this account",
    )
    mode_group.add_argument(
        "--auth-url",
        action="store_true",
        help="Print the OAuth authorization URL for the user to visit",
    )
    mode_group.add_argument(
        "--auth-code",
        metavar="CODE_OR_URL",
        help="Exchange the authorization code (or full redirect URL) for tokens",
    )
    mode_group.add_argument(
        "--revoke",
        action="store_true",
        help="Delete the local token file (does NOT revoke consent on Microsoft's side)",
    )

    parser.add_argument(
        "--tenant-id",
        metavar="ID",
        help="Azure Directory (tenant) ID [required with --configure]",
    )
    parser.add_argument(
        "--client-id",
        metavar="ID",
        help="Azure Application (client) ID [required with --configure]",
    )
    parser.add_argument(
        "--scopes",
        metavar="SCOPES",
        help=(
            "Space-separated delegated scope strings [optional with --configure]. "
            f"Defaults to: {' '.join(DEFAULT_SCOPES)}"
        ),
    )

    args = parser.parse_args()
    account_key: str = args.account

    if args.check:
        sys.exit(0 if check_auth(account_key) else 1)

    elif args.configure:
        if not args.tenant_id:
            parser.error("--configure requires --tenant-id")
        if not args.client_id:
            parser.error("--configure requires --client-id")
        scopes = args.scopes.split() if args.scopes else None
        configure(account_key, tenant_id=args.tenant_id, client_id=args.client_id, scopes=scopes)

    elif args.auth_url:
        get_auth_url(account_key)

    elif args.auth_code:
        exchange_auth_code(account_key, args.auth_code)

    elif args.revoke:
        revoke(account_key)


if __name__ == "__main__":
    main()
