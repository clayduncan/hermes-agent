"""Calendly REST client — read-only awareness + audited booking write.

Read-only methods (CalendlyClient)
-----------------------------------
These require no audit gate; they are purely informational queries used by
Zippy sessions to understand Clay's schedule.

Audited write (CalendlyBookingWriteClient)
------------------------------------------
``create_invitee`` is the only write surface.  It follows the same gate as
the Graph write clients: audit line fsync'd to disk **before** the POST
reaches Calendly's API.

Cloudflare / User-Agent note
-----------------------------
Calendly sits behind Cloudflare.  Python's default urllib ``User-Agent``
(``Python-urllib/3.x``) triggers Cloudflare's bot protection and returns
HTTP 403 with no useful body.  A browser-style UA string passes.  The
``DEFAULT_USER_AGENT`` constant is wired into all requests; override via the
``user_agent`` constructor kwarg if needed.
"""

from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone
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
from tools.write_audit_log import (
    CALENDLY_BOOKINGS,
    AuthorizedWrite,
    WriteAuditRecorder,
    require_trigger,
)

DEFAULT_CALENDLY_BASE_URL = "https://api.calendly.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TokenProvider = Callable[[], str]


# ── Exceptions ────────────────────────────────────────────────────────────────


class CalendlyAPIError(RuntimeError):
    """A Calendly API request failed with a non-2xx response."""

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
        super().__init__(f"Calendly API error {status_code} for {method} {url}: {message}")


class CalendlyRecapsUnavailable(CalendlyAPIError):
    """Meeting recaps are not available on this account's plan (403/404)."""


class CalendlySlotUnavailable(RuntimeError):
    """Calendly rejected POST /invitees with code='already_filled'.

    This is the router's signal to fall back to the Zoom+Graph exception path.
    It is NOT a client error; the slot is simply no longer bookable via Calendly.
    """

    def __init__(self, event_type_uri: str, start_time_iso: str, detail: str = "") -> None:
        self.event_type_uri = event_type_uri
        self.start_time_iso = start_time_iso
        self.detail = detail
        super().__init__(
            f"Calendly slot not available: {start_time_iso} on event type {event_type_uri}"
            + (f" — {detail}" if detail else "")
        )


# ── Read-only client ──────────────────────────────────────────────────────────


class CalendlyClient:
    """Synchronous Calendly REST client for read-only awareness queries."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = DEFAULT_CALENDLY_BASE_URL,
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
            "Authorization": f"Bearer {self.token_provider()}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def _call(self, method: str, url: str, *, params: dict | None = None, json_body: Any = None) -> Any:
        full_url = build_url(url, "", params) if params else url
        return request_json(
            self.request_fn,
            method,
            full_url,
            headers=self._headers(),
            json_body=json_body,
            timeout=self.timeout,
            max_retries=self.max_retries,
            sleep=self.sleep,
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = build_url(self.base_url, path, params)
        return self._call("GET", url)

    def _paginate(self, path: str, collection_key: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages and return the concatenated collection."""
        results: list[dict] = []
        url: str | None = build_url(self.base_url, path, params)
        while url:
            data = self._call("GET", url)
            results.extend(data.get(collection_key) or data.get("collection") or [])
            pagination = data.get("pagination") or {}
            next_token = pagination.get("next_page_token")
            if next_token:
                # Build next URL by adding the page_token to the original path params
                next_params = dict(params or {})
                next_params["page_token"] = next_token
                url = build_url(self.base_url, path, next_params)
            else:
                url = None
        return results

    # ── Public read methods ───────────────────────────────────────────────────

    def get_current_user(self) -> dict:
        """``GET /users/me`` — returns the authenticated user resource."""
        data = self._get("/users/me")
        return data.get("resource") or data

    def list_event_types(self, user_uri: str) -> list[dict]:
        """``GET /event_types`` — all event types for the given user URI."""
        return self._paginate(
            "/event_types",
            "collection",
            params={"user": user_uri, "count": "50"},
        )

    def get_event_type(self, uri_or_uuid: str) -> dict:
        """``GET /event_types/{uuid}`` — accept a full URI or bare UUID."""
        uuid = uri_or_uuid.rstrip("/").split("/")[-1]
        data = self._get(f"/event_types/{uuid}")
        return data.get("resource") or data

    def list_scheduled_events(
        self,
        user_uri: str,
        min_start_time: str,
        max_start_time: str,
        status: str | None = None,
    ) -> list[dict]:
        """``GET /scheduled_events`` — paginate fully."""
        params: dict[str, Any] = {
            "user": user_uri,
            "min_start_time": min_start_time,
            "max_start_time": max_start_time,
            "count": "50",
        }
        if status is not None:
            params["status"] = status
        return self._paginate("/scheduled_events", "collection", params=params)

    def get_event_invitees(self, event_uuid: str) -> list[dict]:
        """``GET /scheduled_events/{uuid}/invitees`` — paginate fully."""
        return self._paginate(
            f"/scheduled_events/{event_uuid}/invitees",
            "collection",
            params={"count": "50"},
        )

    def get_event_type_available_times(
        self, event_type_uri: str, start_time: str, end_time: str
    ) -> list[dict]:
        """``GET /event_type_available_times`` — returns all available slot objects."""
        data = self._get(
            "/event_type_available_times",
            params={
                "event_type": event_type_uri,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        return data.get("collection") or []

    def get_user_availability_schedules(self, user_uri: str) -> list[dict]:
        """``GET /user_availability_schedules`` — returns all schedules for the user."""
        data = self._get(
            "/user_availability_schedules",
            params={"user": user_uri},
        )
        return data.get("collection") or []

    def list_meeting_recaps(self, user_uri: str, **kwargs: Any) -> list[dict]:
        """``GET /scheduling_links`` best-effort — raises CalendlyRecapsUnavailable on 403/404.

        The meeting recaps API is a paid-plan feature.  A 403 or 404 response means this
        account's plan does not include it.  The error is typed so callers can degrade
        gracefully without crashing the whole polling loop.
        """
        try:
            return self._paginate(
                "/scheduling_links",
                "collection",
                params={"user": user_uri, "count": "50", **kwargs},
            )
        except HttpRequestError as exc:
            if exc.status_code in (403, 404):
                raise CalendlyRecapsUnavailable(
                    status_code=exc.status_code,
                    method="GET",
                    url=exc.url,
                    message=(
                        f"Meeting recaps are not available on this account's plan "
                        f"(HTTP {exc.status_code}). Degrade gracefully."
                    ),
                ) from exc
            raise


# ── Audited booking write ─────────────────────────────────────────────────────


class CalendlyBookingWriteClient:
    """Audited Calendly invitee creation — the only write surface for Calendly.

    Follows the same gate as the Graph write clients:

        require_trigger(trigger)
        before  = get_event_type_available_times(...)   # proof slot was checked
        authorized = recorder.authorize_write(...)       # audit line fsync'd, or raise
        POST /invitees                                   # unreachable unless line landed
        authorized.record_outcome(...)

    On ``code: "already_filled"`` (Calendly's rejection code for a non-bookable slot)
    raises :exc:`CalendlySlotUnavailable` — the router's signal to fall back to
    Zoom+Graph, not a hard error.
    """

    DESTINATION = CALENDLY_BOOKINGS
    ACTOR = "calendly_booking_client"

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str = DEFAULT_CALENDLY_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        request_fn: RequestFn = urllib_request,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        log_dir: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.request_fn = request_fn
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.recorder = WriteAuditRecorder(
            destination=self.DESTINATION,
            actor=self.ACTOR,
            log_dir=log_dir,
            clock=clock,
        )
        # A read-only CalendlyClient sharing the same transport/token — used only
        # for the before-fetch inside create_invitee.
        self._reader = CalendlyClient(
            token_provider,
            base_url=base_url,
            user_agent=user_agent,
            request_fn=request_fn,
            timeout=timeout,
            max_retries=max_retries,
            sleep=sleep,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token_provider()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def _post(self, path: str, body: Any) -> HttpResponse:
        """Raw POST — returns the HttpResponse so the caller can inspect the status code."""
        url = build_url(self.base_url, path)
        import json as _json

        body_bytes = _json.dumps(body).encode("utf-8")
        return self.request_fn(
            "POST",
            url,
            headers=self._headers(),
            json_body=body,
            timeout=self.timeout,
        )

    def create_invitee(
        self,
        event_type_uri: str,
        start_time_iso: str,
        invitee: dict,
        location: dict,
        *,
        trigger: str,
    ) -> dict:
        """``POST /invitees`` — gated by the write-audit log.

        ``before`` is the available-times snapshot for the day of the requested slot,
        proving the slot was checked as available before the write was attempted.

        Raises :exc:`CalendlySlotUnavailable` on ``code: "already_filled"`` so the
        router can fall back to the Zoom+Graph exception path cleanly.
        """
        require_trigger(trigger)

        # Derive the day window from start_time_iso for the before snapshot.
        # start_time_iso is expected to be an ISO8601 UTC string like "2026-09-01T14:00:00Z".
        day_start, day_end = _day_window_for(start_time_iso)

        before = self._reader.get_event_type_available_times(
            event_type_uri, day_start, day_end
        )

        authorized = self.recorder.authorize_write(
            operation="create",
            record_id=None,
            before=before,
            trigger=trigger,
        )

        body = {
            "event_type": {"location": location},
            "invitee": invitee,
            "start_time": start_time_iso,
        }

        url = build_url(self.base_url, "/invitees")
        response = self.request_fn(
            "POST",
            url,
            headers=self._headers(),
            json_body=body,
            timeout=self.timeout,
        )

        if response.status_code == 400:
            payload = response.json() or {}
            code = _extract_error_code(payload)
            detail = _extract_error_message(payload)
            if code == "already_filled":
                # Do not record outcome — write never happened at the destination.
                raise CalendlySlotUnavailable(event_type_uri, start_time_iso, detail)
            raise CalendlyAPIError(
                status_code=400,
                method="POST",
                url=url,
                message=detail or "unknown error",
                payload=payload,
            )

        if not (200 <= response.status_code < 300):
            payload = response.json() or {}
            raise CalendlyAPIError(
                status_code=response.status_code,
                method="POST",
                url=url,
                message=_extract_error_message(payload) or response.body.decode("utf-8", errors="replace"),
                payload=payload,
            )

        created = response.json() or {}
        resource = created.get("resource") or created

        record_id = resource.get("uri") or resource.get("uuid")
        authorized.record_outcome(record_id=record_id, after=resource)
        return resource


# ── Helpers ───────────────────────────────────────────────────────────────────


def _day_window_for(start_time_iso: str) -> tuple[str, str]:
    """Return the (day_start, day_end) ISO8601 UTC strings for the calendar day of *start_time_iso*."""
    # Parse the ISO8601 UTC timestamp.  Handle "Z" suffix or "+00:00".
    normalized = start_time_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Fall back: treat as UTC if parsing fails.
        dt = datetime.now(timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    day_start = dt_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = dt_utc.replace(hour=23, minute=59, second=59, microsecond=0)
    return (
        day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _extract_error_code(payload: Any) -> str:
    """Extract the error code from a Calendly error response body."""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("code") or payload.get("error_code") or "").strip()


def _extract_error_message(payload: Any) -> str:
    """Extract a human-readable message from a Calendly error response body."""
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("message")
        or payload.get("error")
        or payload.get("title")
        or ""
    ).strip()


def calendly_token_provider(hermes_home: Path | None = None) -> TokenProvider:
    """Return a callable that reads ``CALENDLY_API_TOKEN`` from ``~/.hermes/.env``.

    Reads the file on every call so a rotated token is picked up without restart.
    """
    from tools.microsoft_graph_delegated_auth import _parse_dotenv, _get_hermes_home

    home = hermes_home or _get_hermes_home()

    def _get_token() -> str:
        env = _parse_dotenv(home / ".env")
        token = env.get("CALENDLY_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "CALENDLY_API_TOKEN not found in ~/.hermes/.env. "
                "Set the key there to enable Calendly access."
            )
        return token

    return _get_token


__all__ = [
    "CalendlyAPIError",
    "CalendlyBookingWriteClient",
    "CalendlyClient",
    "CalendlyRecapsUnavailable",
    "CalendlySlotUnavailable",
    "DEFAULT_CALENDLY_BASE_URL",
    "DEFAULT_USER_AGENT",
    "calendly_token_provider",
]
