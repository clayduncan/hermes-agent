"""OPS-75: create_note() gains an optional title, mirroring how color is
already handled. Reuses the fixtures/helpers from tests/test_ghl_client.py
rather than duplicating the SpyTransport/route/client setup.
"""

from __future__ import annotations

from tests.test_ghl_client import (  # noqa: F401 - fixtures used by pytest
    CALL_NOTE_COLOR,
    NOTE_BODY,
    TRIGGER,
    client,
    log_dir,
    route_create_note,
    transport,
)


class TestCreateNoteTitle:
    def test_default_create_note_call_has_no_title(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note("g-1", NOTE_BODY, trigger=TRIGGER)
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert "title" not in post.json_body

    def test_create_note_writes_the_given_title(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note(
            "g-1", NOTE_BODY, trigger=TRIGGER, title="iMessage · Sent"
        )
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body["title"] == "iMessage · Sent"

    def test_create_note_title_and_color_coexist(self, transport, log_dir) -> None:
        route_create_note(transport)
        client(transport, log_dir).create_note(
            "g-1", NOTE_BODY, trigger=TRIGGER, title="iMessage · Received", color=CALL_NOTE_COLOR
        )
        post = [c for c in transport.writes if c.method == "POST"][0]
        assert post.json_body == {
            "body": NOTE_BODY, "pinned": False,
            "color": CALL_NOTE_COLOR, "title": "iMessage · Received",
        }
