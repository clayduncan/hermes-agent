"""Tests for the loopback callback server added to scripts/microsoft_graph_delegated_setup.py.

Covers:
- Loopback server captures code + state from a real HTTP GET
- Proper HTML response (user-facing "close this tab" message)
- 404 on non-/callback paths
- Clean shutdown (port released after server.shutdown() + server_close())
- Port-already-in-use raises OSError
- get_auth_url timeout exits cleanly (exit 1) without hanging
- State mismatch in callback is rejected (exit 1)
- --auth-code manual-paste path still works via _do_token_exchange
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SETUP_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "microsoft_graph_delegated_setup.py"
)


@pytest.fixture()
def setup_script():
    spec = importlib.util.spec_from_file_location(
        "ms_graph_delegated_setup_lb",
        SETUP_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_config(tmp_path: Path, account_key: str = "test") -> None:
    (tmp_path / f"msgraph_config_{account_key}.json").write_text(
        json.dumps({
            "tenant_id": "tenant-abc",
            "client_id": "client-def",
            "scopes": ["Calendars.ReadWrite", "offline_access"],
        }),
        encoding="utf-8",
    )


# ── Loopback server unit tests ────────────────────────────────────────────────


class TestLoopbackServer:
    def test_callback_captures_code_and_state(self, setup_script):
        server = setup_script._start_loopback_server(port=0)
        port = server.server_address[1]
        try:
            url = f"http://127.0.0.1:{port}/callback?code=TESTCODE&state=TESTSTATE"
            resp = urllib.request.urlopen(url, timeout=5)
            assert resp.status == 200
            assert server.captured_event.wait(timeout=2)
            assert server.captured_params["code"] == ["TESTCODE"]
            assert server.captured_params["state"] == ["TESTSTATE"]
        finally:
            server.shutdown()
            server.server_close()

    def test_callback_responds_with_html_and_close_tab_message(self, setup_script):
        server = setup_script._start_loopback_server(port=0)
        port = server.server_address[1]
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=X&state=Y", timeout=5
            )
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
            assert "text/html" in content_type
            assert "close this tab" in body.lower()
        finally:
            server.shutdown()
            server.server_close()

    def test_non_callback_path_returns_404(self, setup_script):
        server = setup_script._start_loopback_server(port=0)
        port = server.server_address[1]
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/other", timeout=5
                )
            assert exc_info.value.code == 404
        finally:
            server.shutdown()
            server.server_close()

    def test_port_released_after_shutdown(self, setup_script):
        server = setup_script._start_loopback_server(port=0)
        port = server.server_address[1]
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback?code=X&state=Y", timeout=5
        )
        server.captured_event.wait(timeout=2)
        server.shutdown()
        server.server_close()

        # Port should be released — new bind must succeed
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))  # would raise if still held

    def test_port_conflict_raises_oserror(self, setup_script):
        server1 = setup_script._start_loopback_server(port=0)
        port = server1.server_address[1]
        try:
            with pytest.raises(OSError):
                setup_script._start_loopback_server(port=port)
        finally:
            server1.shutdown()
            server1.server_close()

    def test_event_not_set_before_callback(self, setup_script):
        server = setup_script._start_loopback_server(port=0)
        try:
            assert not server.captured_event.is_set()
        finally:
            server.shutdown()
            server.server_close()


# ── get_auth_url timeout ──────────────────────────────────────────────────────


class TestLoopbackTimeout:
    def test_timeout_exits_1_without_hanging(self, tmp_path, setup_script, monkeypatch):
        """--auth-url with a very short timeout exits 1 without hanging the test suite."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        _write_config(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            setup_script.get_auth_url("test", loopback_timeout=0, _loopback_port=0)
        assert exc_info.value.code == 1

    def test_timeout_keeps_pending_session_for_auth_code_fallback(self, tmp_path, setup_script, monkeypatch):
        """On timeout the pending session file is kept so --auth-code can still be used."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        _write_config(tmp_path)

        with pytest.raises(SystemExit):
            setup_script.get_auth_url("test", loopback_timeout=0, _loopback_port=0)

        assert (tmp_path / "msgraph_pending_test.json").exists()

    def test_no_orphaned_listener_after_timeout(self, tmp_path, setup_script, monkeypatch):
        """After a timeout the loopback port is released (not still listening)."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        _write_config(tmp_path)

        bound_port: list[int] = []
        real_start = setup_script._start_loopback_server

        def tracking_start(port=0):
            s = real_start(port)
            bound_port.append(s.server_address[1])
            return s

        monkeypatch.setattr(setup_script, "_start_loopback_server", tracking_start)

        with pytest.raises(SystemExit):
            setup_script.get_auth_url("test", loopback_timeout=0, _loopback_port=0)

        assert bound_port, "Server was never started"
        port = bound_port[0]

        # Give the OS a moment to reclaim the port, then verify it's free
        time.sleep(0.05)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))


# ── State mismatch on loopback path ──────────────────────────────────────────


class TestStateMismatch:
    def test_wrong_state_in_callback_exits_1(self, tmp_path, setup_script, monkeypatch):
        """A callback with a mismatched state is rejected; process exits 1."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        _write_config(tmp_path)

        result: list[int | None] = [None]

        def run():
            try:
                setup_script.get_auth_url("test", loopback_timeout=10, _loopback_port=0)
            except SystemExit as e:
                result[0] = e.code

        t = threading.Thread(target=run, daemon=True)
        t.start()

        # Wait until the server is listening, then send a bad state
        deadline = time.monotonic() + 5.0
        port = None
        while time.monotonic() < deadline:
            pending = tmp_path / "msgraph_pending_test.json"
            if pending.exists():
                try:
                    data = json.loads(pending.read_text())
                    # Derive the port from the saved redirect_uri
                    import urllib.parse as _up
                    parsed = _up.urlparse(data.get("redirect_uri", ""))
                    if parsed.port:
                        port = parsed.port
                        break
                except Exception:
                    pass
            time.sleep(0.05)

        assert port is not None, "Server never wrote pending session"
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=VALIDCODE&state=WRONG_STATE",
                timeout=5,
            )
        except Exception:
            pass

        t.join(timeout=5)
        assert result[0] == 1, f"Expected exit 1 on state mismatch, got {result[0]}"

    def test_missing_pending_session_after_state_mismatch(self, tmp_path, setup_script, monkeypatch):
        """Pending session is deleted after a state mismatch (security: must not reuse)."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        _write_config(tmp_path)

        result: list[int | None] = [None]

        def run():
            try:
                setup_script.get_auth_url("test", loopback_timeout=10, _loopback_port=0)
            except SystemExit as e:
                result[0] = e.code

        t = threading.Thread(target=run, daemon=True)
        t.start()

        deadline = time.monotonic() + 5.0
        port = None
        while time.monotonic() < deadline:
            pending = tmp_path / "msgraph_pending_test.json"
            if pending.exists():
                try:
                    data = json.loads(pending.read_text())
                    import urllib.parse as _up
                    parsed = _up.urlparse(data.get("redirect_uri", ""))
                    if parsed.port:
                        port = parsed.port
                        break
                except Exception:
                    pass
            time.sleep(0.05)

        assert port is not None
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=X&state=WRONG",
                timeout=5,
            )
        except Exception:
            pass

        t.join(timeout=5)
        # After state mismatch, pending session must be removed
        assert not (tmp_path / "msgraph_pending_test.json").exists()


# ── Manual --auth-code fallback still works ───────────────────────────────────


class TestManualAuthCodeFallback:
    def test_exchange_auth_code_calls_do_token_exchange(self, tmp_path, setup_script, monkeypatch):
        """--auth-code (manual paste) path loads the pending session and calls _do_token_exchange."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "dummy-secret")

        (tmp_path / "msgraph_pending_test.json").write_text(
            json.dumps({
                "state": "MYSTATE",
                "code_verifier": "MYVERIFIER",
                "redirect_uri": "https://login.microsoftonline.com/common/oauth2/nativeclient",
                "tenant_id": "tenant-aaa",
                "client_id": "client-bbb",
                "scopes": ["Calendars.ReadWrite", "offline_access"],
            }),
            encoding="utf-8",
        )

        calls: list[tuple[str, str]] = []

        def fake_do_token_exchange(account_key: str, code: str, pending: dict) -> None:
            calls.append((account_key, code))

        monkeypatch.setattr(setup_script, "_do_token_exchange", fake_do_token_exchange)

        url = (
            "https://login.microsoftonline.com/common/oauth2/nativeclient"
            "?code=REALCODE&state=MYSTATE"
        )
        setup_script.exchange_auth_code("test", url)
        assert calls == [("test", "REALCODE")]

    def test_exchange_auth_code_with_raw_code_still_works(self, tmp_path, setup_script, monkeypatch):
        """--auth-code accepts a raw code string (not a full URL) as before."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "dummy-secret")

        (tmp_path / "msgraph_pending_test.json").write_text(
            json.dumps({
                "state": "MYSTATE",
                "code_verifier": "MYVERIFIER",
                "redirect_uri": "https://login.microsoftonline.com/common/oauth2/nativeclient",
                "tenant_id": "tenant-aaa",
                "client_id": "client-bbb",
                "scopes": ["offline_access"],
            }),
            encoding="utf-8",
        )

        calls: list[tuple[str, str]] = []

        monkeypatch.setattr(
            setup_script,
            "_do_token_exchange",
            lambda account_key, code, pending: calls.append((account_key, code)),
        )

        setup_script.exchange_auth_code("test", "M.RAWCODE123")
        assert calls == [("test", "M.RAWCODE123")]

    def test_exchange_auth_code_state_mismatch_exits_1(self, tmp_path, setup_script, monkeypatch):
        """--auth-code with a URL whose state doesn't match the pending session exits 1."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "dummy-secret")

        (tmp_path / "msgraph_pending_test.json").write_text(
            json.dumps({
                "state": "CORRECTSTATE",
                "code_verifier": "V",
                "redirect_uri": "https://login.microsoftonline.com/common/oauth2/nativeclient",
                "tenant_id": "t",
                "client_id": "c",
                "scopes": [],
            }),
            encoding="utf-8",
        )

        calls: list = []
        monkeypatch.setattr(
            setup_script,
            "_do_token_exchange",
            lambda *a, **kw: calls.append(True),
        )

        url = (
            "https://login.microsoftonline.com/common/oauth2/nativeclient"
            "?code=CODE&state=WRONGSTATE"
        )
        with pytest.raises(SystemExit) as exc_info:
            setup_script.exchange_auth_code("test", url)
        assert exc_info.value.code == 1
        assert not calls, "_do_token_exchange must not be called on state mismatch"


# ── _do_token_exchange: public vs confidential client POST body ───────────────


def _make_success_urlopen(captured: list):
    """urllib.request.urlopen stub — records the Request and returns a valid token payload."""
    payload = json.dumps({
        "access_token": "tok-abc",
        "refresh_token": "ref-xyz",
        "expires_in": 3600,
        "scope": "Calendars.ReadWrite offline_access",
        "token_type": "Bearer",
    }).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    return fake_urlopen


def _make_pending_session(redirect_uri="http://localhost:8765/callback"):
    return {
        "state": "STATE",
        "code_verifier": "VERIFIER123",
        "redirect_uri": redirect_uri,
        "tenant_id": "tenant-aaa",
        "client_id": "client-bbb",
        "scopes": ["Calendars.ReadWrite", "offline_access"],
    }


class TestTokenExchangeClientSecretHandling:
    """Verify that _do_token_exchange omits/includes client_secret per public/confidential mode."""

    def test_public_client_post_body_has_no_client_secret_key(self, tmp_path, setup_script, monkeypatch):
        """AADSTS700025 fix: public-client exchange must NOT send client_secret at all."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_CLAYDUNCAN_CLIENT_SECRET", raising=False)

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _make_success_urlopen(captured))

        setup_script._do_token_exchange("clayduncan", "AUTH-CODE", _make_pending_session())

        assert captured, "urlopen was not called"
        body = urllib.parse.parse_qs(captured[0].data.decode("utf-8"))
        assert "client_secret" not in body, (
            "Public client must not include client_secret — Azure returns AADSTS700025 if present"
        )
        assert body["code_verifier"] == ["VERIFIER123"]
        assert body["grant_type"] == ["authorization_code"]

    def test_confidential_client_post_body_includes_secret(self, tmp_path, setup_script, monkeypatch):
        """Confidential-client exchange includes client_secret in the POST body."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "confidential-secret-value")

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _make_success_urlopen(captured))

        setup_script._do_token_exchange("test", "AUTH-CODE", _make_pending_session())

        assert captured, "urlopen was not called"
        body = urllib.parse.parse_qs(captured[0].data.decode("utf-8"))
        assert body.get("client_secret") == ["confidential-secret-value"]

    def test_public_client_exchange_still_writes_token_file(self, tmp_path, setup_script, monkeypatch):
        """Token exchange without client_secret still writes a valid token file."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_CLAYDUNCAN_CLIENT_SECRET", raising=False)

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _make_success_urlopen(captured))

        setup_script._do_token_exchange("clayduncan", "CODE", _make_pending_session())

        token_path = tmp_path / "msgraph_token_clayduncan.json"
        assert token_path.exists()
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        assert saved["access_token"] == "tok-abc"
        assert "client_secret" not in saved
