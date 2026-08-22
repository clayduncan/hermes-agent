"""Zoom Server-to-Server OAuth client for creating scheduled meetings.

Not write-audit-gated: Zoom meeting creation is an ephemeral resource (a join URL
and passcode) rather than a durable record mutation in Clay's personal data stores.
The audit gate is reserved for the Graph calendar event that references the Zoom
link — see :class:`tools.msgraph_write_client.MicrosoftGraphCalendarEventsWriteClient`.
This scoping decision is documented in the build report and can be overruled.

Token lifecycle
---------------
Server-to-Server OAuth issues short-lived access tokens (typically 1 hour).
The provider caches the token in memory with a :data:`DEFAULT_TOKEN_SKEW_SECONDS`
safety margin (matching the Graph auth convention) so a near-expiry token is never
used.  The token file is never written to disk — it exists only in the process's
memory.

Credentials (read from ``~/.hermes/.env``, never from ``os.environ``):
  ``ZOOM_ACCOUNT_ID``
  ``ZOOM_CLIENT_ID``
  ``ZOOM_CLIENT_SECRET``
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.sync_json_http import (
    HttpRequestError,
    HttpResponse,
    RequestFn,
    build_url,
    request_json,
    urllib_request,
)

# Reuse the same skew constant as the Graph token providers.
from tools.microsoft_graph_delegated_auth import DEFAULT_TOKEN_SKEW_SECONDS

ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
DEFAULT_ZOOM_BASE_URL = "https://api.zoom.us/v2"
DEFAULT_USER_AGENT = "Hermes-Agent/zoom-client"


# ── Exceptions ────────────────────────────────────────────────────────────────


class ZoomAPIError(RuntimeError):
    """A Zoom API request failed with a non-2xx response."""

    def __init__(
        self,
        status_code: int,
        method: str,
        url: str,
        message: str,
        *,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.message = message
        self.payload = payload
        super().__init__(f"Zoom API error {status_code} for {method} {url}: {message}")


# ── Token provider ────────────────────────────────────────────────────────────


@dataclass
class _CachedZoomToken:
    access_token: str
    expires_at: float

    def is_valid(self, skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS) -> bool:
        return self.expires_at > (time.time() + max(0, skew_seconds))


class ZoomTokenProvider:
    """Server-to-Server OAuth token provider for the Zoom API.

    Reads ``ZOOM_ACCOUNT_ID``, ``ZOOM_CLIENT_ID``, and ``ZOOM_CLIENT_SECRET``
    from ``~/.hermes/.env``.  Caches the token in memory; re-fetches when
    within :data:`DEFAULT_TOKEN_SKEW_SECONDS` of expiry.

    The ``_token_request_fn`` kwarg is injected by tests to avoid live HTTP.
    """

    def __init__(
        self,
        hermes_home: Path | None = None,
        *,
        skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS,
        _token_request_fn: Callable[[str, str, str], _CachedZoomToken] | None = None,
    ) -> None:
        from tools.microsoft_graph_delegated_auth import _get_hermes_home
        self._hermes_home = hermes_home or _get_hermes_home()
        self._skew_seconds = skew_seconds
        self._cached: _CachedZoomToken | None = None
        self._token_request_fn = _token_request_fn

    def _load_credentials(self) -> tuple[str, str, str]:
        from tools.microsoft_graph_delegated_auth import _parse_dotenv
        env = _parse_dotenv(self._hermes_home / ".env")
        account_id = env.get("ZOOM_ACCOUNT_ID", "").strip()
        client_id = env.get("ZOOM_CLIENT_ID", "").strip()
        client_secret = env.get("ZOOM_CLIENT_SECRET", "").strip()
        if not account_id or not client_id or not client_secret:
            raise RuntimeError(
                "ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, and ZOOM_CLIENT_SECRET must all be "
                "set in ~/.hermes/.env for Zoom Server-to-Server OAuth."
            )
        return account_id, client_id, client_secret

    def _fetch_token(self, account_id: str, client_id: str, client_secret: str) -> _CachedZoomToken:
        if self._token_request_fn is not None:
            return self._token_request_fn(account_id, client_id, client_secret)

        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        url = (
            f"{ZOOM_TOKEN_URL}?grant_type=account_credentials&account_id={urllib.parse.quote(account_id)}"
        )
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

        if status >= 400:
            msg = payload.get("reason") or payload.get("error") or body.decode("utf-8", errors="replace")
            raise ZoomAPIError(status, "POST", ZOOM_TOKEN_URL, str(msg), payload=payload)

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise ZoomAPIError(200, "POST", ZOOM_TOKEN_URL, "No access_token in Zoom OAuth response")
        expires_in = int(payload.get("expires_in") or 3600)
        return _CachedZoomToken(
            access_token=access_token,
            expires_at=time.time() + max(0, expires_in),
        )

    def get_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._cached and self._cached.is_valid(self._skew_seconds):
            return self._cached.access_token
        if self._token_request_fn is not None:
            # Test mode: fake fn handles the token without needing real credentials.
            self._cached = self._fetch_token("", "", "")
        else:
            account_id, client_id, client_secret = self._load_credentials()
            self._cached = self._fetch_token(account_id, client_id, client_secret)
        return self._cached.access_token


# ── Zoom API client ───────────────────────────────────────────────────────────


class ZoomClient:
    """Minimal Zoom API client for creating scheduled meetings."""

    def __init__(
        self,
        token_provider: ZoomTokenProvider,
        *,
        base_url: str = DEFAULT_ZOOM_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        request_fn: RequestFn = urllib_request,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.request_fn = request_fn
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token_provider.get_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _call(self, method: str, path: str, *, json_body: Any = None) -> Any:
        url = build_url(self.base_url, path)
        return request_json(
            self.request_fn,
            method,
            url,
            headers=self._headers(),
            json_body=json_body,
            timeout=self.timeout,
            max_retries=self.max_retries,
            sleep=self.sleep,
        )

    def create_meeting(
        self,
        topic: str,
        start_time_iso: str,
        timezone_name: str,
        duration_minutes: int,
        *,
        invitee_emails: list[str],
        join_before_host: bool = True,
        waiting_room: bool = False,
    ) -> dict:
        """``POST /users/me/meetings`` — create a scheduled meeting (type=2).

        Returns the raw Zoom response dict containing ``join_url``, ``password``, ``id``.
        """
        body: dict[str, Any] = {
            "topic": topic,
            "type": 2,
            "start_time": start_time_iso,
            "timezone": timezone_name,
            "duration": duration_minutes,
            "settings": {
                "join_before_host": join_before_host,
                "waiting_room": waiting_room,
                "registrants_email_notification": True,
            },
        }
        if invitee_emails:
            body["settings"]["alternative_hosts"] = ""
            # Zoom doesn't support an invitee list on create; we pass them to the
            # Graph calendar event as attendees instead.
        result = self._call("POST", "/users/me/meetings", json_body=body)
        if not isinstance(result, dict):
            raise ZoomAPIError(200, "POST", build_url(self.base_url, "/users/me/meetings"), "Unexpected response shape")
        return result


__all__ = [
    "DEFAULT_ZOOM_BASE_URL",
    "ZoomAPIError",
    "ZoomClient",
    "ZoomTokenProvider",
]
