"""Tests for tools/microsoft_graph_delegated_auth.py and the setup script CLI."""

from __future__ import annotations

import importlib.util
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from tools.microsoft_graph_delegated_auth import (
    DEFAULT_TOKEN_SKEW_SECONDS,
    DelegatedGraphReconsentNeeded,
    DelegatedGraphSetupNeeded,
    DelegatedTokenFile,
    DelegatedTokenProvider,
)


SETUP_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "microsoft_graph_delegated_setup.py"
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_token_file(
    account_key: str = "testaccount",
    *,
    expires_in: float = 3600,
    scopes: list[str] | None = None,
) -> DelegatedTokenFile:
    return DelegatedTokenFile(
        account_key=account_key,
        tenant_id="tenant-123",
        client_id="client-456",
        access_token="access-token-abc",
        refresh_token="refresh-token-xyz",
        expires_at=time.time() + expires_in,
        scopes=scopes or ["Calendars.ReadWrite", "offline_access"],
    )


def _make_provider(
    account_key: str,
    tmp_path: Path,
    *,
    secret: str | None = "test-secret",
    **kwargs,
) -> DelegatedTokenProvider:
    environ = (
        {f"MSGRAPH_{account_key.upper()}_CLIENT_SECRET": secret}
        if secret is not None
        else {}
    )
    return DelegatedTokenProvider(
        account_key=account_key,
        hermes_home=tmp_path,
        environ=environ,
        **kwargs,
    )


# ── DelegatedTokenFile round-trip ─────────────────────────────────────────────


class TestDelegatedTokenFileRoundTrip:
    def test_save_and_load_preserves_all_fields(self, tmp_path):
        original = _make_token_file()
        path = tmp_path / "msgraph_token_testaccount.json"
        original.save(path)

        loaded = DelegatedTokenFile.load(path)

        assert loaded.account_key == original.account_key
        assert loaded.tenant_id == original.tenant_id
        assert loaded.client_id == original.client_id
        assert loaded.access_token == original.access_token
        assert loaded.refresh_token == original.refresh_token
        assert abs(loaded.expires_at - original.expires_at) < 0.001
        assert loaded.scopes == original.scopes

    def test_client_secret_not_written_to_disk(self, tmp_path):
        token = _make_token_file()
        path = tmp_path / "msgraph_token_testaccount.json"
        token.save(path)
        contents = path.read_text(encoding="utf-8")
        assert "client_secret" not in contents
        assert "secret" not in contents.lower()

    def test_save_is_atomic_uses_tmp_then_replace(self, tmp_path):
        token = _make_token_file()
        path = tmp_path / "msgraph_token_testaccount.json"
        token.save(path)
        assert path.exists()
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

    def test_load_missing_file_raises_setup_needed(self, tmp_path):
        path = tmp_path / "msgraph_token_nonexistent.json"
        with pytest.raises(DelegatedGraphSetupNeeded):
            DelegatedTokenFile.load(path)

    def test_load_corrupt_json_raises_token_error(self, tmp_path):
        from tools.microsoft_graph_auth import MicrosoftGraphTokenError
        path = tmp_path / "msgraph_token_bad.json"
        path.write_text("not-valid-json", encoding="utf-8")
        with pytest.raises(MicrosoftGraphTokenError):
            DelegatedTokenFile.load(path)

    def test_load_missing_required_field_raises_token_error(self, tmp_path):
        from tools.microsoft_graph_auth import MicrosoftGraphTokenError
        path = tmp_path / "msgraph_token_partial.json"
        path.write_text(
            json.dumps({"account_key": "x", "tenant_id": "y"}),  # missing most fields
            encoding="utf-8",
        )
        with pytest.raises(MicrosoftGraphTokenError):
            DelegatedTokenFile.load(path)


# ── DelegatedTokenFile expiry ─────────────────────────────────────────────────


class TestDelegatedTokenFileExpiry:
    def test_fresh_token_is_not_expired(self):
        token = _make_token_file(expires_in=3600)
        assert not token.is_expired()

    def test_past_expiry_token_is_expired(self):
        token = _make_token_file(expires_in=-1)
        assert token.is_expired(skew_seconds=0)

    def test_skew_makes_soon_expiring_token_appear_expired(self):
        token = _make_token_file(expires_in=60)
        assert not token.is_expired(skew_seconds=0)
        assert token.is_expired(skew_seconds=120)


# ── DelegatedTokenProvider.get_access_token ───────────────────────────────────


@pytest.mark.anyio
class TestDelegatedTokenProviderGetAccessToken:
    async def test_returns_token_from_file_when_valid(self, tmp_path):
        token = _make_token_file(account_key="acct")
        token.save(tmp_path / "msgraph_token_acct.json")

        provider = _make_provider("acct", tmp_path)
        result = await provider.get_access_token()
        assert result == "access-token-abc"

    async def test_caches_token_in_memory_after_first_call(self, tmp_path):
        token = _make_token_file(account_key="acct")
        token.save(tmp_path / "msgraph_token_acct.json")

        provider = _make_provider("acct", tmp_path)
        first = await provider.get_access_token()
        # Remove the file; second call must use cache
        (tmp_path / "msgraph_token_acct.json").unlink()
        second = await provider.get_access_token()
        assert first == second == "access-token-abc"

    async def test_missing_token_file_raises_setup_needed(self, tmp_path):
        provider = _make_provider("newacct", tmp_path)
        with pytest.raises(DelegatedGraphSetupNeeded):
            await provider.get_access_token()

    async def test_expired_token_triggers_refresh(self, tmp_path):
        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "refreshed-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "scope": "Calendars.ReadWrite offline_access",
                    "token_type": "Bearer",
                },
            )

        provider = _make_provider("acct", tmp_path, transport=httpx.MockTransport(handler))
        result = await provider.get_access_token()
        assert result == "refreshed-token"

        # Refreshed token must be persisted to disk
        saved = DelegatedTokenFile.load(tmp_path / "msgraph_token_acct.json")
        assert saved.access_token == "refreshed-token"
        assert saved.refresh_token == "new-refresh"

    async def test_force_refresh_bypasses_valid_cache(self, tmp_path):
        token = _make_token_file(account_key="acct")
        token.save(tmp_path / "msgraph_token_acct.json")
        # Warm the cache
        provider = _make_provider("acct", tmp_path)
        await provider.get_access_token()

        refresh_calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            refresh_calls.append(1)
            return httpx.Response(
                200,
                json={
                    "access_token": "force-refreshed",
                    "refresh_token": "refresh-token-xyz",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        provider._transport = httpx.MockTransport(handler)
        result = await provider.get_access_token(force_refresh=True)
        assert result == "force-refreshed"
        assert refresh_calls == [1]

    async def test_revoked_refresh_token_raises_reconsent_needed(self, tmp_path):
        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "AADSTS70008: The refresh token has expired.",
                },
            )

        provider = _make_provider("acct", tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(DelegatedGraphReconsentNeeded) as exc_info:
            await provider.get_access_token()
        assert "invalid_grant" in str(exc_info.value).lower() or "revoked" in str(exc_info.value).lower()

    async def test_non_revocation_refresh_error_raises_token_error(self, tmp_path):
        from tools.microsoft_graph_auth import MicrosoftGraphTokenError

        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"error": "server_error", "error_description": "Internal error"},
            )

        provider = _make_provider("acct", tmp_path, transport=httpx.MockTransport(handler))
        with pytest.raises(MicrosoftGraphTokenError):
            await provider.get_access_token()

    async def test_public_client_refresh_without_secret_succeeds(self, tmp_path):
        """Public-client mode: no MSGRAPH_*_CLIENT_SECRET → refresh succeeds without it."""
        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "public-client-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        # secret=None → public-client mode: no client_secret env var at all
        provider = _make_provider("acct", tmp_path, secret=None, transport=httpx.MockTransport(handler))
        result = await provider.get_access_token()
        assert result == "public-client-token"

    async def test_refresh_post_body_excludes_client_secret_in_public_mode(self, tmp_path):
        """Public-client refresh POST must NOT include client_secret (not even empty)."""
        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        captured_bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content.decode("utf-8"))
            return httpx.Response(200, json={
                "access_token": "tok", "refresh_token": "ref",
                "expires_in": 3600, "token_type": "Bearer",
            })

        provider = _make_provider("acct", tmp_path, secret=None, transport=httpx.MockTransport(handler))
        await provider.get_access_token()

        assert captured_bodies, "No HTTP request was made"
        body = urllib.parse.parse_qs(captured_bodies[0])
        assert "client_secret" not in body, "client_secret key must be absent, not just empty"

    async def test_refresh_post_body_includes_client_secret_in_confidential_mode(self, tmp_path):
        """Confidential-client refresh POST MUST include the client_secret."""
        expired = _make_token_file(account_key="acct", expires_in=-100)
        expired.save(tmp_path / "msgraph_token_acct.json")

        captured_bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(request.content.decode("utf-8"))
            return httpx.Response(200, json={
                "access_token": "tok", "refresh_token": "ref",
                "expires_in": 3600, "token_type": "Bearer",
            })

        provider = _make_provider("acct", tmp_path, secret="my-secret", transport=httpx.MockTransport(handler))
        await provider.get_access_token()

        assert captured_bodies, "No HTTP request was made"
        body = urllib.parse.parse_qs(captured_bodies[0])
        assert body.get("client_secret") == ["my-secret"]


# ── Multi-account isolation ───────────────────────────────────────────────────


@pytest.mark.anyio
class TestMultiAccountIsolation:
    async def test_two_accounts_use_separate_token_files(self, tmp_path):
        token_a = _make_token_file(account_key="alpha", expires_in=3600)
        token_a.save(tmp_path / "msgraph_token_alpha.json")

        token_b = DelegatedTokenFile(
            account_key="beta",
            tenant_id="tenant-beta",
            client_id="client-beta",
            access_token="beta-access-token",
            refresh_token="beta-refresh",
            expires_at=time.time() + 3600,
            scopes=["Mail.ReadWrite", "offline_access"],
        )
        token_b.save(tmp_path / "msgraph_token_beta.json")

        provider_a = DelegatedTokenProvider(
            account_key="alpha",
            hermes_home=tmp_path,
            environ={"MSGRAPH_ALPHA_CLIENT_SECRET": "secret-a"},
        )
        provider_b = DelegatedTokenProvider(
            account_key="beta",
            hermes_home=tmp_path,
            environ={"MSGRAPH_BETA_CLIENT_SECRET": "secret-b"},
        )

        result_a = await provider_a.get_access_token()
        result_b = await provider_b.get_access_token()

        assert result_a == "access-token-abc"
        assert result_b == "beta-access-token"
        assert result_a != result_b

    async def test_clear_cache_on_one_account_does_not_affect_another(self, tmp_path):
        for key, token_val in (("alpha", "tok-alpha"), ("beta", "tok-beta")):
            t = DelegatedTokenFile(
                account_key=key,
                tenant_id=f"tenant-{key}",
                client_id=f"client-{key}",
                access_token=token_val,
                refresh_token=f"refresh-{key}",
                expires_at=time.time() + 3600,
                scopes=["offline_access"],
            )
            t.save(tmp_path / f"msgraph_token_{key}.json")

        provider_a = DelegatedTokenProvider(
            account_key="alpha",
            hermes_home=tmp_path,
            environ={"MSGRAPH_ALPHA_CLIENT_SECRET": "s"},
        )
        provider_b = DelegatedTokenProvider(
            account_key="beta",
            hermes_home=tmp_path,
            environ={"MSGRAPH_BETA_CLIENT_SECRET": "s"},
        )

        await provider_a.get_access_token()
        await provider_b.get_access_token()
        assert provider_b._cached_token is not None

        provider_a.clear_cache()
        assert provider_a._cached_token is None
        # Beta cache must be unaffected
        assert provider_b._cached_token is not None
        assert provider_b._cached_token.access_token == "tok-beta"

    def test_token_file_names_are_keyed_by_account(self, tmp_path):
        provider_a = DelegatedTokenProvider(account_key="alpha", hermes_home=tmp_path)
        provider_b = DelegatedTokenProvider(account_key="beta", hermes_home=tmp_path)
        assert provider_a.token_path != provider_b.token_path
        assert "alpha" in str(provider_a.token_path)
        assert "beta" in str(provider_b.token_path)


# ── MicrosoftGraphClient duck-typing compatibility ────────────────────────────


@pytest.mark.anyio
class TestClientDuckTypingCompatibility:
    async def test_delegated_provider_works_with_graph_client(self, tmp_path):
        """DelegatedTokenProvider is duck-type compatible with MicrosoftGraphClient."""
        from tools.microsoft_graph_client import MicrosoftGraphClient

        token = _make_token_file(account_key="acct")
        token.save(tmp_path / "msgraph_token_acct.json")

        captured_auth: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_auth.append(request.headers.get("Authorization", ""))
            return httpx.Response(200, json={"value": "ok"})

        provider = _make_provider("acct", tmp_path)
        client = MicrosoftGraphClient(provider, transport=httpx.MockTransport(handler))
        result = await client.get_json("/me")

        assert result == {"value": "ok"}
        assert captured_auth == ["Bearer access-token-abc"]


# ── Setup script CLI paths ────────────────────────────────────────────────────


@pytest.fixture()
def setup_script():
    spec = importlib.util.spec_from_file_location(
        "ms_graph_delegated_setup",
        SETUP_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSetupScriptCLI:
    def test_check_returns_false_when_no_token_file(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        result = setup_script.check_auth("newaccount")
        assert result is False

    def test_check_returns_true_when_valid_token_exists(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        token = _make_token_file(account_key="testaccount", expires_in=3600)
        token.save(tmp_path / "msgraph_token_testaccount.json")
        result = setup_script.check_auth("testaccount")
        assert result is True

    def test_configure_saves_config_file(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "dummy-secret")
        setup_script.configure(
            "test",
            tenant_id="tenant-aaa",
            client_id="client-bbb",
            scopes=["Calendars.ReadWrite", "offline_access"],
        )
        config_path = tmp_path / "msgraph_config_test.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["tenant_id"] == "tenant-aaa"
        assert data["client_id"] == "client-bbb"
        assert "offline_access" in data["scopes"]

    def test_configure_adds_offline_access_if_missing(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "dummy-secret")
        setup_script.configure(
            "test",
            tenant_id="t",
            client_id="c",
            scopes=["Calendars.ReadWrite"],  # no offline_access
        )
        data = json.loads((tmp_path / "msgraph_config_test.json").read_text(encoding="utf-8"))
        assert "offline_access" in data["scopes"]

    def test_auth_url_creates_pending_session(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        # Pre-populate config
        (tmp_path / "msgraph_config_test.json").write_text(
            json.dumps(
                {
                    "tenant_id": "tenant-abc",
                    "client_id": "client-def",
                    "scopes": ["Calendars.ReadWrite", "offline_access"],
                }
            ),
            encoding="utf-8",
        )
        # get_auth_url now blocks for the loopback callback; use loopback_timeout=0
        # and a dynamic port (port=0) to avoid binding the fixed production port.
        # On timeout, it exits 1 but keeps the pending session on disk for --auth-code fallback.
        with pytest.raises(SystemExit) as exc_info:
            setup_script.get_auth_url("test", loopback_timeout=0, _loopback_port=0)
        assert exc_info.value.code == 1
        pending = tmp_path / "msgraph_pending_test.json"
        assert pending.exists()
        data = json.loads(pending.read_text(encoding="utf-8"))
        assert data["state"]
        assert data["code_verifier"]
        assert data["tenant_id"] == "tenant-abc"
        # Redirect URI must be the loopback one, not the old nativeclient URI
        assert "localhost" in data["redirect_uri"]
        assert "/callback" in data["redirect_uri"]

    def test_revoke_deletes_token_and_pending_files(self, tmp_path, setup_script, monkeypatch):
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        token_path = tmp_path / "msgraph_token_test.json"
        pending_path = tmp_path / "msgraph_pending_test.json"
        token_path.write_text("{}", encoding="utf-8")
        pending_path.write_text("{}", encoding="utf-8")

        setup_script.revoke("test")

        assert not token_path.exists()
        assert not pending_path.exists()

    def test_extract_code_and_state_parses_raw_code(self, setup_script):
        code, state = setup_script._extract_code_and_state("M.RAWCODE123")
        assert code == "M.RAWCODE123"
        assert state is None

    def test_extract_code_and_state_parses_full_url(self, setup_script):
        url = (
            "https://login.microsoftonline.com/common/oauth2/nativeclient"
            "?code=MYCODE&state=MYSTATE"
        )
        code, state = setup_script._extract_code_and_state(url)
        assert code == "MYCODE"
        assert state == "MYSTATE"

    def test_pkce_verifier_and_challenge_are_distinct(self, setup_script):
        verifier, challenge = setup_script._generate_pkce()
        assert verifier != challenge
        assert len(verifier) > 20
        assert len(challenge) > 20

    def test_configure_succeeds_without_client_secret(self, tmp_path, setup_script, monkeypatch):
        """Public-client: configure works even when no MSGRAPH_*_CLIENT_SECRET is set."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_TEST_CLIENT_SECRET", raising=False)
        setup_script.configure(
            "test",
            tenant_id="tenant-aaa",
            client_id="client-bbb",
            scopes=["Calendars.ReadWrite", "offline_access"],
        )
        data = json.loads((tmp_path / "msgraph_config_test.json").read_text(encoding="utf-8"))
        assert data["tenant_id"] == "tenant-aaa"

    def test_configure_reports_public_mode_without_secret(self, tmp_path, setup_script, monkeypatch, capsys):
        """configure() prints 'public-client' when no secret env var is set."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_TEST_CLIENT_SECRET", raising=False)
        setup_script.configure("test", tenant_id="t", client_id="c")
        out = capsys.readouterr().out
        assert "public-client" in out

    def test_configure_reports_confidential_mode_with_secret(self, tmp_path, setup_script, monkeypatch, capsys):
        """configure() prints 'confidential-client' when secret env var IS set."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "super-secret")
        setup_script.configure("test", tenant_id="t", client_id="c")
        out = capsys.readouterr().out
        assert "confidential-client" in out


# ── _do_token_exchange POST body (public vs confidential client) ───────────────


def _fake_urlopen_success(captured_requests: list):
    """Return a urllib.request.urlopen stub that records the Request and returns a token payload."""
    payload = json.dumps({
        "access_token": "tok-123",
        "refresh_token": "ref-456",
        "expires_in": 3600,
        "scope": "Calendars.ReadWrite offline_access",
        "token_type": "Bearer",
    }).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured_requests.append(req)
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    return fake_urlopen


class TestDoTokenExchange:
    """Verify the exact POST body sent by _do_token_exchange for public vs confidential clients."""

    def _make_pending(self) -> dict:
        return {
            "state": "STATE",
            "code_verifier": "VERIFIER",
            "redirect_uri": "http://localhost:8765/callback",
            "tenant_id": "tenant-aaa",
            "client_id": "client-bbb",
            "scopes": ["Calendars.ReadWrite", "offline_access"],
        }

    def test_public_client_post_body_omits_client_secret(self, tmp_path, setup_script, monkeypatch):
        """When MSGRAPH_*_CLIENT_SECRET is unset, client_secret must NOT appear in the POST body."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_TEST_CLIENT_SECRET", raising=False)

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_success(captured))

        setup_script._do_token_exchange("test", "AUTH-CODE-XYZ", self._make_pending())

        assert captured, "urlopen was not called"
        body = urllib.parse.parse_qs(captured[0].data.decode("utf-8"))
        assert "client_secret" not in body, "client_secret must be absent for public clients"
        assert body["code_verifier"] == ["VERIFIER"]
        assert body["code"] == ["AUTH-CODE-XYZ"]

    def test_confidential_client_post_body_includes_client_secret(self, tmp_path, setup_script, monkeypatch):
        """When MSGRAPH_*_CLIENT_SECRET is set, client_secret MUST appear in the POST body."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("MSGRAPH_TEST_CLIENT_SECRET", "super-secret-value")

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_success(captured))

        setup_script._do_token_exchange("test", "AUTH-CODE-XYZ", self._make_pending())

        assert captured, "urlopen was not called"
        body = urllib.parse.parse_qs(captured[0].data.decode("utf-8"))
        assert body.get("client_secret") == ["super-secret-value"]

    def test_public_client_exchange_saves_token_file(self, tmp_path, setup_script, monkeypatch):
        """Token exchange in public-client mode still saves the token file on success."""
        monkeypatch.setattr(setup_script, "_get_hermes_home", lambda: tmp_path)
        monkeypatch.delenv("MSGRAPH_TEST_CLIENT_SECRET", raising=False)

        captured: list = []
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_success(captured))

        setup_script._do_token_exchange("test", "CODE", self._make_pending())

        token_path = tmp_path / "msgraph_token_test.json"
        assert token_path.exists(), "Token file not written after public-client exchange"
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        assert saved["access_token"] == "tok-123"
        assert "client_secret" not in saved
