"""Microsoft Graph delegated (user-consent) authentication helpers.

Supports multiple independent Microsoft accounts simultaneously.  Each account
has its own token file, its own Azure app-registration credentials, and its own
set of granted delegated scopes.  There is zero shared state between accounts.

Duck-typing compatibility with MicrosoftGraphClient:
  DelegatedTokenProvider implements the same interface as MicrosoftGraphTokenProvider:
    - async get_access_token(*, force_refresh: bool = False) -> str
    - clear_cache() -> None
  Pass a DelegatedTokenProvider directly as the MicrosoftGraphClient token_provider
  argument — no new REST client or wrapper needed.

Auth flow (--auth-url):
  The setup script starts a local HTTP listener on LOOPBACK_CALLBACK_PORT (8765),
  prints the authorization URL for the agent to relay to the user, and blocks until
  the browser redirects back to http://localhost:8765/callback with the auth code.
  No manual copy-paste step required.

  To use this flow, register http://localhost:8765/callback as a redirect URI in your
  Azure app's "Mobile and desktop applications" platform alongside the nativeclient URI.

Token file location: ~/.hermes/msgraph_token_<account_key>.json
Env vars per account (uppercase account_key in prefix):
  MSGRAPH_<KEY>_TENANT_ID
  MSGRAPH_<KEY>_CLIENT_ID
  MSGRAPH_<KEY>_CLIENT_SECRET   (optional; omit for public-client / PKCE-only apps)

Public-client vs. confidential-client mode:
  Azure AD treats an app as a public client when the redirect_uri used in the token
  exchange matches a "Mobile and desktop applications" platform entry (including the
  http://localhost:<port>/callback loopback URI and the legacy nativeclient URI).
  Public clients authenticate via PKCE only — sending client_secret triggers AADSTS700025.

  When MSGRAPH_<KEY>_CLIENT_SECRET is set: confidential-client mode (secret included in
  every token and refresh request).  When it is unset: public-client mode (no secret
  sent; code_verifier / refresh_token are the sole proof of possession).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tools.microsoft_graph_auth import (
    MicrosoftGraphAuthError,
    MicrosoftGraphConfigError,
    MicrosoftGraphTokenError,
    _extract_error_detail,
)

DEFAULT_GRAPH_AUTHORITY_URL = "https://login.microsoftonline.com"
# Legacy "native client" redirect URI — kept for backward compat and as a fallback constant.
# New flows use the loopback listener (LOOPBACK_REDIRECT_URI) instead.
DEFAULT_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"
# Loopback redirect URI for native/installed apps (Microsoft's current recommendation).
# Register EXACTLY this string in your Azure app → Authentication → Mobile and desktop
# applications platform.  Port 8765 is fixed so only one URI entry is needed.
LOOPBACK_CALLBACK_PORT = 8765
LOOPBACK_REDIRECT_URI = f"http://localhost:{LOOPBACK_CALLBACK_PORT}/callback"
DEFAULT_TOKEN_SKEW_SECONDS = 120
_OFFLINE_ACCESS = "offline_access"


class DelegatedGraphSetupNeeded(MicrosoftGraphAuthError):
    """No token file found for this account.

    The delegated setup flow has not been completed yet.  Run the setup
    script (--auth-url / --auth-code) to perform interactive consent and
    obtain the initial token.
    """


class DelegatedGraphReconsentNeeded(MicrosoftGraphAuthError):
    """The stored refresh token is invalid or revoked.

    Full re-consent is required.  Run the setup script --revoke to clear
    the stale token file, then --auth-url / --auth-code to re-authenticate.
    """


def _get_hermes_home() -> Path:
    """Return the Hermes home directory.  Mirrors hermes_constants.get_hermes_home()."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except (ModuleNotFoundError, ImportError):
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"


@dataclass
class DelegatedTokenFile:
    """Parsed contents of a per-account delegated token file.

    The client_secret is NOT stored here — it lives only in the env.
    """

    account_key: str
    tenant_id: str
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: list[str]

    def is_expired(self, *, skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS) -> bool:
        return self.expires_at <= (time.time() + max(0, skew_seconds))

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_key": self.account_key,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": self.scopes,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path) -> "DelegatedTokenFile":
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise DelegatedGraphSetupNeeded(
                f"No Microsoft Graph token found at {path}. "
                "Run the delegated setup flow (--auth-url / --auth-code) to authenticate."
            ) from exc
        except OSError as exc:
            raise MicrosoftGraphTokenError(
                f"Could not read Microsoft Graph token file at {path}: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph token file at {path} is not valid JSON: {exc}"
            ) from exc

        try:
            return cls(
                account_key=str(data["account_key"]),
                tenant_id=str(data["tenant_id"]),
                client_id=str(data["client_id"]),
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
                expires_at=float(data["expires_at"]),
                scopes=list(data.get("scopes") or []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph token file at {path} is corrupt or missing required fields: {exc}"
            ) from exc


class DelegatedTokenProvider:
    """Acquire and cache Microsoft Graph delegated access tokens from a stored token file.

    Duck-typing compatible with MicrosoftGraphTokenProvider — pass directly as
    the token_provider argument to MicrosoftGraphClient.

    Token files are stored at ~/.hermes/msgraph_token_<account_key>.json.
    The client_secret is never written to disk; it is always read from the env
    at the time a refresh is needed.
    """

    def __init__(
        self,
        account_key: str,
        *,
        hermes_home: Path | None = None,
        environ: dict[str, str] | None = None,
        timeout: float = 20.0,
        skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_key = account_key
        self._environ: dict[str, str] | os._Environ = (  # type: ignore[type-arg]
            environ if environ is not None else os.environ
        )
        self.timeout = timeout
        self.skew_seconds = max(0, int(skew_seconds))
        self._transport = transport
        self._hermes_home = hermes_home or _get_hermes_home()
        self._cached_token: DelegatedTokenFile | None = None
        self._lock = asyncio.Lock()

    @property
    def token_path(self) -> Path:
        return self._hermes_home / f"msgraph_token_{self.account_key}.json"

    def _get_client_secret_optional(self) -> str | None:
        """Return the client secret from env, or None for public-client mode (no secret)."""
        key = f"MSGRAPH_{self.account_key.upper()}_CLIENT_SECRET"
        return (self._environ.get(key) or "").strip() or None

    def clear_cache(self) -> None:
        self._cached_token = None

    def inspect_token_health(self) -> dict[str, Any]:
        cached = self._cached_token
        file_exists = self.token_path.exists()
        on_disk: DelegatedTokenFile | None = None
        if file_exists and cached is None:
            try:
                on_disk = DelegatedTokenFile.load(self.token_path)
            except (DelegatedGraphSetupNeeded, MicrosoftGraphTokenError):
                pass
        active = cached or on_disk
        return {
            "account_key": self.account_key,
            "token_path": str(self.token_path),
            "token_file_exists": file_exists,
            "cached": cached is not None,
            "expires_in_seconds": active.expires_in_seconds if active else None,
            "is_expired": active.is_expired(skew_seconds=0) if active else None,
            "refresh_skew_seconds": self.skew_seconds,
            "scopes": active.scopes if active else None,
        }

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        # Fast path: valid in-memory cached token
        cached = self._cached_token
        if not force_refresh and cached and not cached.is_expired(skew_seconds=self.skew_seconds):
            return cached.access_token

        async with self._lock:
            # Double-check after acquiring lock
            cached = self._cached_token
            if not force_refresh and cached and not cached.is_expired(
                skew_seconds=self.skew_seconds
            ):
                return cached.access_token

            # Load from disk — raises DelegatedGraphSetupNeeded if no file
            token_file = DelegatedTokenFile.load(self.token_path)

            if force_refresh or token_file.is_expired(skew_seconds=self.skew_seconds):
                token_file = await self._refresh_access_token(token_file)
                token_file.save(self.token_path)

            self._cached_token = token_file
            return token_file.access_token

    async def _refresh_access_token(self, token_file: DelegatedTokenFile) -> DelegatedTokenFile:
        client_secret = self._get_client_secret_optional()
        token_url = (
            f"{DEFAULT_GRAPH_AUTHORITY_URL}/{token_file.tenant_id}/oauth2/v2.0/token"
        )
        scope_str = " ".join(token_file.scopes) if token_file.scopes else _OFFLINE_ACCESS
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "client_id": token_file.client_id,
            "refresh_token": token_file.refresh_token,
            "scope": scope_str,
        }
        if client_secret:
            data["client_secret"] = client_secret

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            transport=self._transport,
        ) as client:
            response = await client.post(
                token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            # Check for invalid_grant (revoked/expired refresh token) directly on the raw JSON
            try:
                error_code = (response.json().get("error") or "") if response.content else ""
            except ValueError:
                error_code = ""

            if error_code == "invalid_grant":
                raise DelegatedGraphReconsentNeeded(
                    f"Microsoft Graph refresh token for account '{self.account_key}' is revoked "
                    f"or expired. Run --revoke then --auth-url / --auth-code to re-authenticate. "
                    f"Server detail: {detail}"
                )
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph token refresh failed for account '{self.account_key}' "
                f"with HTTP {response.status_code}: {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph refresh response for account '{self.account_key}' "
                "was not valid JSON."
            ) from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph refresh response for account '{self.account_key}' "
                "did not include access_token."
            )

        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise MicrosoftGraphTokenError(
                f"Microsoft Graph refresh response for account '{self.account_key}' "
                "did not include a valid expires_in."
            ) from exc

        # Microsoft may rotate the refresh token; prefer the new one if present.
        new_refresh_token = str(payload.get("refresh_token") or "").strip() or token_file.refresh_token

        # Scopes from response supersede stored ones when present.
        scope_raw = str(payload.get("scope") or "").strip()
        new_scopes = scope_raw.split() if scope_raw else token_file.scopes

        return DelegatedTokenFile(
            account_key=token_file.account_key,
            tenant_id=token_file.tenant_id,
            client_id=token_file.client_id,
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_at=time.time() + max(0, expires_in),
            scopes=new_scopes,
        )
