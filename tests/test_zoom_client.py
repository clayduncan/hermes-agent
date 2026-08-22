"""Tests for ZoomTokenProvider and ZoomClient.

All tests use fakes — no live Zoom API calls.  The token provider's
``_token_request_fn`` kwarg is injected to avoid HTTP in tests.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.write_audit_http import SpyTransport, json_response
from tools.sync_json_http import HttpResponse
from tools.zoom_client import (
    DEFAULT_ZOOM_BASE_URL,
    ZoomAPIError,
    ZoomClient,
    ZoomTokenProvider,
    _CachedZoomToken,
)
from tools.microsoft_graph_delegated_auth import DEFAULT_TOKEN_SKEW_SECONDS

# Real captured POST /users/me/meetings response — live 2026-08-22 Zoom API call.
# id is a JSON integer (83320363126); booking_router.py coerces it with str(id).
MEETING_RESPONSE = {
    "uuid": "FgWHPqBhTvKVCqOpNMNa/A==",
    "id": 83320363126,
    "host_id": "Abc1234xYzDEF56789GH",
    "host_email": "clay@clayduncan.com",
    "topic": "Meeting with Levi Duncan",
    "type": 2,
    "status": "waiting",
    "start_time": "2026-08-22T15:00:00Z",
    "duration": 30,
    "timezone": "America/Chicago",
    "agenda": "",
    "created_at": "2026-08-22T18:34:17Z",
    "start_url": "https://us06web.zoom.us/s/83320363126?zak=eyJ0eX...XrOg",
    "join_url": "https://us06web.zoom.us/j/83320363126?pwd=rJl3CQYHHziGK02nizWDEj4cmnbFFU.1",
    "chat_join_url": "https://us06web.zoom.us/j/83320363126?pwd=rJl3CQYHHziGK02nizWDEj4cmnbFFU.1",
    "password": "106747",
    "h323_password": "106747",
    "pstn_password": "106747",
    "encrypted_password": "rJl3CQYHHziGK02nizWDEj4cmnbFFU.1",
    "settings": {
        "join_before_host": True,
        "waiting_room": False,
        "mute_upon_entry": False,
        "meeting_authentication": False,
        "registrants_email_notification": True,
        "alternative_hosts": "",
    },
    "pre_schedule": False,
    "creation_source": "open_api",
    "jbh_time": 0,
    "meeting_invitees": [],
    "private_meeting": False,
}


def fake_token_fn(account_id: str, client_id: str, client_secret: str) -> _CachedZoomToken:
    """Fake token fetcher — returns a token valid for 1 hour without HTTP."""
    return _CachedZoomToken(
        access_token="fake-zoom-token",
        expires_at=time.time() + 3600,
    )


def make_token_provider(**kwargs) -> ZoomTokenProvider:
    return ZoomTokenProvider(_token_request_fn=fake_token_fn, **kwargs)


def make_client(transport: SpyTransport, **kwargs) -> ZoomClient:
    return ZoomClient(
        make_token_provider(),
        request_fn=transport,
        sleep=lambda _: None,
        **kwargs,
    )


@pytest.fixture
def transport() -> SpyTransport:
    return SpyTransport(DEFAULT_ZOOM_BASE_URL)


# ── Token provider ────────────────────────────────────────────────────────────


class TestZoomTokenProvider:
    def test_get_token_returns_access_token(self) -> None:
        provider = make_token_provider()
        # Inject credentials so _load_credentials doesn't need a .env file.
        provider._load_credentials = lambda: ("acc", "cid", "csec")  # type: ignore
        token = provider.get_token()
        assert token == "fake-zoom-token"

    def test_token_is_cached_in_memory(self) -> None:
        call_count = 0

        def counting_token_fn(account_id, client_id, client_secret):
            nonlocal call_count
            call_count += 1
            return _CachedZoomToken("tok", time.time() + 3600)

        provider = ZoomTokenProvider(_token_request_fn=counting_token_fn)
        provider._load_credentials = lambda: ("acc", "cid", "csec")  # type: ignore

        provider.get_token()
        provider.get_token()
        provider.get_token()

        assert call_count == 1, "Token should be fetched once and then cached"

    def test_expired_token_is_refreshed(self) -> None:
        call_count = 0

        def counting_token_fn(account_id, client_id, client_secret):
            nonlocal call_count
            call_count += 1
            return _CachedZoomToken("tok", time.time() + 3600)

        provider = ZoomTokenProvider(_token_request_fn=counting_token_fn)
        provider._load_credentials = lambda: ("acc", "cid", "csec")  # type: ignore

        # Pre-seed with an expired token.
        provider._cached = _CachedZoomToken("old-tok", time.time() - 1)

        token = provider.get_token()
        assert token == "tok"
        assert call_count == 1

    def test_token_within_skew_is_treated_as_expired(self) -> None:
        call_count = 0

        def counting_token_fn(account_id, client_id, client_secret):
            nonlocal call_count
            call_count += 1
            return _CachedZoomToken("fresh-tok", time.time() + 3600)

        provider = ZoomTokenProvider(
            _token_request_fn=counting_token_fn,
            skew_seconds=DEFAULT_TOKEN_SKEW_SECONDS,
        )
        provider._load_credentials = lambda: ("acc", "cid", "csec")  # type: ignore

        # Token expires within the skew window — should be treated as stale.
        provider._cached = _CachedZoomToken("near-expiry", time.time() + DEFAULT_TOKEN_SKEW_SECONDS - 5)
        token = provider.get_token()
        assert token == "fresh-tok"


# ── ZoomClient.create_meeting ─────────────────────────────────────────────────


class TestZoomClientCreateMeeting:
    def test_create_meeting_returns_zoom_response(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        client = make_client(transport)
        result = client.create_meeting(
            topic="Meeting with Alice",
            start_time_iso="2026-09-01T14:00:00Z",
            timezone_name="UTC",
            duration_minutes=30,
            invitee_emails=["alice@example.com"],
        )
        assert result["join_url"] == MEETING_RESPONSE["join_url"]
        assert result["password"] == MEETING_RESPONSE["password"]
        assert result["id"] == MEETING_RESPONSE["id"]

    def test_create_meeting_id_is_integer(self, transport: SpyTransport) -> None:
        """Zoom returns id as a JSON integer; booking_router.py coerces with str(id)."""
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        result = make_client(transport).create_meeting(
            "T", "2026-09-01T14:00:00Z", "UTC", 30, invitee_emails=[]
        )
        assert isinstance(result["id"], int)
        assert result["id"] == 83320363126
        assert str(result["id"]) == "83320363126"

    def test_create_meeting_sends_type_2(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        make_client(transport).create_meeting(
            topic="Test",
            start_time_iso="2026-09-01T14:00:00Z",
            timezone_name="America/Chicago",
            duration_minutes=60,
            invitee_emails=[],
        )
        call = transport.writes[0]
        assert call.json_body["type"] == 2

    def test_create_meeting_includes_topic_and_duration(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        make_client(transport).create_meeting(
            topic="My Meeting",
            start_time_iso="2026-09-01T14:00:00Z",
            timezone_name="UTC",
            duration_minutes=45,
            invitee_emails=[],
        )
        body = transport.writes[0].json_body
        assert body["topic"] == "My Meeting"
        assert body["duration"] == 45

    def test_create_meeting_sets_join_before_host_default_true(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        make_client(transport).create_meeting(
            "T", "2026-09-01T14:00:00Z", "UTC", 30, invitee_emails=[]
        )
        settings = transport.writes[0].json_body["settings"]
        assert settings["join_before_host"] is True
        assert settings["waiting_room"] is False

    def test_create_meeting_waiting_room_override(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        make_client(transport).create_meeting(
            "T", "2026-09-01T14:00:00Z", "UTC", 30,
            invitee_emails=[],
            join_before_host=False,
            waiting_room=True,
        )
        settings = transport.writes[0].json_body["settings"]
        assert settings["join_before_host"] is False
        assert settings["waiting_room"] is True

    def test_request_carries_bearer_token(self, transport: SpyTransport) -> None:
        transport.route("POST", "/users/me/meetings", MEETING_RESPONSE)
        make_client(transport).create_meeting(
            "T", "2026-09-01T14:00:00Z", "UTC", 30, invitee_emails=[]
        )
        auth = transport.writes[0].headers["Authorization"]
        assert auth == "Bearer fake-zoom-token"

    def test_api_error_on_non_2xx(self, transport: SpyTransport) -> None:
        transport.route(
            "POST", "/users/me/meetings",
            HttpResponse(status_code=401, body=b'{"code":124,"message":"Invalid access token."}'),
        )
        with pytest.raises(Exception):
            make_client(transport, max_retries=0).create_meeting(
                "T", "2026-09-01T14:00:00Z", "UTC", 30, invitee_emails=[]
            )
