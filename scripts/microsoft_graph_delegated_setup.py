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

Agent workflow (new loopback flow):
  1. Run --check --account KEY.  Exit 0 = auth good, skip setup.
  2. Run --configure --account KEY --tenant-id TENANT_ID --client-id CLIENT_ID.
     Set MSGRAPH_<KEY>_CLIENT_SECRET in ~/.hermes/.env for confidential-client mode,
     or leave it absent for public-client mode (PKCE only).  Sourcing/exporting the
     .env file is NOT required — the script reads it directly from disk.
  3. Run --auth-url --account KEY.  This is now a SINGLE BLOCKING OPERATION:
     a. A local HTTP listener starts on port 8765 (loopback only).
     b. The authorization URL is printed — relay it to the user.
     c. The user opens the URL in their browser and approves the consent screen.
     d. The browser is redirected to http://localhost:8765/callback automatically.
     e. The script captures the code, exchanges it for tokens, and exits.
     No manual code-paste step is required.
  4. Run --check --account KEY to verify.  Done.

Fallback (manual paste, for when port 8765 is unavailable):
  3a. Run --auth-url ... (prints URL, exits immediately if port is blocked — not yet
      implemented as a separate mode; if the loopback server cannot bind, the script
      errors with a clear message).
  3b. Use --auth-code "CODE_OR_URL" --account KEY to paste the code manually after
      completing a separate --auth-url invocation that saved a pending session.

~/.hermes/.env keys per account (uppercase account_key in prefix):
  MSGRAPH_<KEY>_TENANT_ID     — Azure Directory (tenant) ID
  MSGRAPH_<KEY>_CLIENT_ID     — Azure Application (client) ID
  MSGRAPH_<KEY>_CLIENT_SECRET — Client secret (OPTIONAL — only for confidential clients)

  These are read directly from ~/.hermes/.env on each invocation.  Sourcing or
  exporting the file into the shell environment is not required and has no effect.

Public-client vs confidential-client mode:
  If MSGRAPH_<KEY>_CLIENT_SECRET is absent from ~/.hermes/.env, the account runs in
    public-client mode: no client_secret is sent in token-exchange or refresh requests;
    the PKCE code_verifier (or refresh_token) is the sole proof of possession.  This is
    required for apps whose Azure registration has a "Mobile and desktop applications"
    platform entry (loopback or nativeclient redirect URI) — Azure enforces public-client
    semantics for those redirect URIs and returns AADSTS700025 if a secret is sent.

  If MSGRAPH_<KEY>_CLIENT_SECRET IS present in ~/.hermes/.env, the account runs in
    confidential-client mode: the secret is included in every /token POST.  Use this
    only if the Azure app registration uses a "Web" platform redirect URI (not loopback
    / nativeclient).

Example for the 'clayduncan' account (public client — no secret needed):
  MSGRAPH_CLAYDUNCAN_TENANT_ID=d14d6257-4cf6-45a2-83fd-5d218bda8aee
  MSGRAPH_CLAYDUNCAN_CLIENT_ID=1bb196a6-47c2-40d0-be2a-f38ddb007820
  # MSGRAPH_CLAYDUNCAN_CLIENT_SECRET — leave unset; app is registered as public client
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
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
    LOOPBACK_CALLBACK_PORT,
    DelegatedGraphReconsentNeeded,
    DelegatedGraphSetupNeeded,
    DelegatedTokenFile,
    DelegatedTokenProvider,
    _get_hermes_home,
    _parse_dotenv,
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
    os.chmod(path, 0o600)


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


def _get_client_secret(account_key: str) -> str | None:
    """Return the client secret from ~/.hermes/.env, or None (public-client mode).

    Reads MSGRAPH_<KEY>_CLIENT_SECRET directly from the .env file on disk.  The
    inherited shell environment (os.environ) is never consulted — only the current
    on-disk file contents are authoritative.  This eliminates the class of bugs where
    a stale shell-exported value from a prior session overrides a corrected .env file.
    """
    key = f"MSGRAPH_{account_key.upper()}_CLIENT_SECRET"
    dotenv = _parse_dotenv(_get_hermes_home() / ".env")
    return dotenv.get(key, "").strip() or None


# ── Loopback HTTP listener ────────────────────────────────────────────────────


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth callback on /callback."""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.captured_params = urllib.parse.parse_qs(parsed.query)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body>"
            b"<h2>Authorization complete.</h2>"
            b"<p>You can close this tab and return to your terminal.</p>"
            b"</body></html>"
        )
        self.server.captured_event.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default per-request logging


def _start_loopback_server(port: int = LOOPBACK_CALLBACK_PORT) -> http.server.HTTPServer:
    """Bind a loopback HTTP server on the given port and start serving in a daemon thread.

    Returns the server object.  Attributes set on the server:
      .captured_event  — threading.Event that fires when /callback is received
      .captured_params — dict of parse_qs results from the callback query string

    Raises OSError if the port is already in use (HTTPServer sets SO_REUSEADDR,
    so TIME_WAIT sockets do not block rebinding).
    """
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.captured_params: dict = {}  # type: ignore[attr-defined]
    server.captured_event = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── Token exchange (shared by loopback and manual-paste paths) ────────────────


def _do_token_exchange(account_key: str, code: str, pending: dict) -> None:
    """Exchange an authorization code for tokens and save them to disk.

    `pending` must contain: code_verifier, redirect_uri, tenant_id, client_id, scopes.
    `code` is the already-extracted authorization code (not a full URL).
    """
    import time as _time

    client_secret = _get_client_secret(account_key)  # str | None; None = public-client mode
    tenant_id: str = pending["tenant_id"]
    client_id: str = pending["client_id"]
    scopes: list[str] = pending.get("scopes") or DEFAULT_SCOPES

    token_url = f"{DEFAULT_GRAPH_AUTHORITY_URL}/{tenant_id}/oauth2/v2.0/token"
    post_body: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["code_verifier"],
        "scope": " ".join(scopes),
    }
    if client_secret:
        post_body["client_secret"] = client_secret
    post_data = urllib.parse.urlencode(post_body).encode("utf-8")

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
    _pending_path(account_key).unlink(missing_ok=True)

    print(f"OK: Authenticated. Token saved to {dest}")
    print(f"  Account   : {account_key}")
    print(f"  Tenant ID : {tenant_id}")
    print(f"  Scopes    : {', '.join(granted_scopes)}")
    print(f"\nVerify with: python scripts/microsoft_graph_delegated_setup.py --check --account {account_key}")


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
    client_secret = _get_client_secret(account_key)
    auth_mode = (
        "public-client (PKCE only — MSGRAPH_{}_CLIENT_SECRET not set)".format(account_key.upper())
        if client_secret is None
        else "confidential-client (client secret present in env)"
    )

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
    print(f"  Auth mode : {auth_mode}")
    print(f"\nNext step: run --auth-url --account {account_key}")


def get_auth_url(account_key: str, *, loopback_timeout: int = 300, _loopback_port: int = LOOPBACK_CALLBACK_PORT) -> None:
    """Start a loopback listener, print the auth URL, and block until the browser completes consent.

    This is a single blocking operation.  The process exits when:
      - The user completes consent in their browser and tokens are saved (exit 0).
      - No callback is received within loopback_timeout seconds (exit 1).
        The pending session is kept on disk so --auth-code can still be used as fallback.
      - An error occurs (exit 1).

    The loopback listener binds on 127.0.0.1 (never accessible remotely).
    The redirect URI in the authorize request is http://localhost:<port>/callback,
    which must be registered in Azure as a "Mobile and desktop applications" platform URI.
    """
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

    # Start the loopback server before building the URL so port availability is
    # confirmed before we print anything to the agent.
    try:
        server = _start_loopback_server(_loopback_port)
    except OSError as exc:
        print(
            f"ERROR: Could not bind loopback listener on port {_loopback_port}: {exc}\n"
            f"Something else is using that port.  Free port {_loopback_port} or use the\n"
            f"manual fallback: complete the browser flow then run\n"
            f"  python scripts/microsoft_graph_delegated_setup.py "
            f'--auth-code "PASTED_CODE_OR_URL" --account {account_key}'
        )
        sys.exit(1)

    actual_port = server.server_address[1]
    loopback_redirect_uri = f"http://localhost:{actual_port}/callback"

    authorize_url = f"{DEFAULT_GRAPH_AUTHORITY_URL}/{tenant_id}/oauth2/v2.0/authorize"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": loopback_redirect_uri,
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
        redirect_uri=loopback_redirect_uri,
        tenant_id=tenant_id,
        client_id=client_id,
        scopes=scopes,
    )

    print(full_url)
    print(
        f"\n[Agent note] Relay this URL to the user. After they approve consent in their "
        f"browser, it will automatically redirect to the local listener on port {actual_port}. "
        f"No code-paste step needed — this process will complete automatically once consent "
        f"is granted (waiting up to {loopback_timeout // 60} minutes).",
        file=sys.stderr,
    )

    received = server.captured_event.wait(timeout=loopback_timeout)
    server.shutdown()
    server.server_close()

    if not received:
        # Keep the pending session so --auth-code can still be used as a manual fallback.
        print(
            f"TIMEOUT: No browser callback received within {loopback_timeout // 60} minutes.\n"
            "The pending session has been kept. If the user completed consent you can still\n"
            "try manually pasting the code with:\n"
            f"  python scripts/microsoft_graph_delegated_setup.py "
            f'--auth-code "PASTED_CODE_OR_URL" --account {account_key}'
        )
        sys.exit(1)

    params_received = server.captured_params
    if "error" in params_received:
        error = (params_received.get("error") or ["unknown"])[0]
        desc = (params_received.get("error_description") or [""])[0]
        _pending_path(account_key).unlink(missing_ok=True)
        print(f"ERROR: Authorization server returned error '{error}': {desc}")
        sys.exit(1)

    if "code" not in params_received:
        _pending_path(account_key).unlink(missing_ok=True)
        print("ERROR: No 'code' parameter in callback URL.")
        sys.exit(1)

    code = params_received["code"][0]
    returned_state = (params_received.get("state") or [None])[0]

    if returned_state and returned_state != state:
        _pending_path(account_key).unlink(missing_ok=True)
        print(
            "ERROR: OAuth state mismatch — possible CSRF or stale session. "
            "Run --auth-url again to start a fresh session."
        )
        sys.exit(1)

    # Use in-memory pending dict (already saved to disk above for crash recovery).
    pending = {
        "state": state,
        "code_verifier": code_verifier,
        "redirect_uri": loopback_redirect_uri,
        "tenant_id": tenant_id,
        "client_id": client_id,
        "scopes": scopes,
    }
    _do_token_exchange(account_key, code, pending)


def exchange_auth_code(account_key: str, code_or_url: str) -> None:
    """Exchange the authorization code for tokens and save them.

    This is the --auth-code manual-paste fallback path.  The primary path is
    --auth-url which captures the code automatically via the loopback listener.

    Loads the pending session written by a prior --auth-url invocation.
    """
    pending = _load_pending_session(account_key)
    code, returned_state = _extract_code_and_state(code_or_url)

    if returned_state and returned_state != pending["state"]:
        print(
            "ERROR: OAuth state mismatch — possible CSRF or stale session. "
            "Run --auth-url again to start a fresh session."
        )
        sys.exit(1)

    _do_token_exchange(account_key, code, pending)


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
        help=(
            "Start a loopback listener, print the OAuth authorization URL, "
            "and block until the browser completes consent (up to 5 minutes)"
        ),
    )
    mode_group.add_argument(
        "--auth-code",
        metavar="CODE_OR_URL",
        help=(
            "Fallback: exchange the authorization code (or full redirect URL) for tokens. "
            "Requires a pending session from a prior --auth-url invocation."
        ),
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
