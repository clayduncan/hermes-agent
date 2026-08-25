"""Tests for tools/msgraph_cert_token_provider.py.

All tests are hermetic — no live Keychain, Graph, or Entra calls.
Password retrieval and HTTP are injected through constructor arguments.
PFX handling uses real cryptography (in-memory RSA key and self-signed cert
generated at test-module load time) so the full load/sign path is exercised
without touching disk credentials or certificate stores.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import cryptography.hazmat.primitives.asymmetric.rsa as _rsa
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import serialize_key_and_certificates
from cryptography.x509.oid import NameOID

from tools.msgraph_cert_token_provider import (
    MicrosoftGraphCertAuthError,
    MicrosoftGraphCertConfigError,
    MicrosoftGraphCertTokenError,
    MicrosoftGraphCertTokenProvider,
)


# ── Test PFX fixture (generated once per module) ──────────────────────────────


def _make_test_pfx_bytes(password: bytes = b"testpassword") -> bytes:
    """Generate an in-memory RSA 2048-bit key + self-signed cert as PFX bytes."""
    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test Graph Cert"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )
    return serialize_key_and_certificates(
        name=None, key=private_key, cert=cert, cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password),
    )


_TEST_PFX_PASSWORD = b"testpassword"
_TEST_PFX_BYTES = _make_test_pfx_bytes(_TEST_PFX_PASSWORD)

TENANT_ID = "tenant-abc123"
CLIENT_ID = "client-def456"
KEYCHAIN_SERVICE = "test-svc"
KEYCHAIN_ACCOUNT = "test-acct"


def _fake_keychain_success(*_args, **_kwargs):
    """Subprocess stub that returns the test PFX password on stdout."""
    import subprocess as _sp

    class _Result:
        returncode = 0
        stdout = _TEST_PFX_PASSWORD + b"\n"

    return _Result()


def _fake_keychain_failure(*_args, **_kwargs):
    class _Result:
        returncode = 1
        stdout = b""

    return _Result()


def _make_fake_token_response(
    audience: str = "https://graph.microsoft.com",
    expires_in: int = 3600,
) -> bytes:
    """Build a fake token endpoint response with a real signed JWT."""
    # Use a fresh RSA key for the token — we just need valid JWT structure.
    key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    token = jwt.encode(
        {"aud": audience, "iss": "sts.windows.net", "exp": now + expires_in, "iat": now},
        pem,
        algorithm="RS256",
    )
    payload = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    return json.dumps(payload).encode("utf-8")


def _provider(
    tmp_path: Path,
    *,
    http_post=None,
    keychain_fn=None,
) -> MicrosoftGraphCertTokenProvider:
    pfx_path = tmp_path / "test.pfx"
    pfx_path.write_bytes(_TEST_PFX_BYTES)

    if keychain_fn is None:
        keychain_fn = _fake_keychain_success

    provider = MicrosoftGraphCertTokenProvider(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        pfx_path=pfx_path,
        keychain_service=KEYCHAIN_SERVICE,
        keychain_account=KEYCHAIN_ACCOUNT,
        skew_seconds=120,
        _http_post=http_post or (lambda url, data: _make_fake_token_response()),
    )

    # Patch subprocess.run in the provider's module to use our fake.
    provider._get_pfx_password = lambda: _TEST_PFX_PASSWORD  # type: ignore[method-assign]
    return provider


# ── Configuration validation ──────────────────────────────────────────────────


class TestConfigValidation:
    def test_missing_tenant_id_raises_config_error(self, tmp_path):
        pfx = tmp_path / "x.pfx"
        pfx.write_bytes(_TEST_PFX_BYTES)
        with pytest.raises(MicrosoftGraphCertConfigError, match="tenant_id"):
            MicrosoftGraphCertTokenProvider(
                tenant_id="",
                client_id=CLIENT_ID,
                pfx_path=pfx,
                keychain_service=KEYCHAIN_SERVICE,
                keychain_account=KEYCHAIN_ACCOUNT,
            )

    def test_missing_client_id_raises_config_error(self, tmp_path):
        pfx = tmp_path / "x.pfx"
        pfx.write_bytes(_TEST_PFX_BYTES)
        with pytest.raises(MicrosoftGraphCertConfigError, match="client_id"):
            MicrosoftGraphCertTokenProvider(
                tenant_id=TENANT_ID,
                client_id="  ",
                pfx_path=pfx,
                keychain_service=KEYCHAIN_SERVICE,
                keychain_account=KEYCHAIN_ACCOUNT,
            )

    def test_missing_keychain_service_raises_config_error(self, tmp_path):
        pfx = tmp_path / "x.pfx"
        pfx.write_bytes(_TEST_PFX_BYTES)
        with pytest.raises(MicrosoftGraphCertConfigError, match="keychain_service"):
            MicrosoftGraphCertTokenProvider(
                tenant_id=TENANT_ID,
                client_id=CLIENT_ID,
                pfx_path=pfx,
                keychain_service="",
                keychain_account=KEYCHAIN_ACCOUNT,
            )

    def test_missing_pfx_raises_config_error(self, tmp_path):
        with pytest.raises(MicrosoftGraphCertConfigError, match="PFX file"):
            MicrosoftGraphCertTokenProvider(
                tenant_id=TENANT_ID,
                client_id=CLIENT_ID,
                pfx_path=tmp_path / "nonexistent.pfx",
                keychain_service=KEYCHAIN_SERVICE,
                keychain_account=KEYCHAIN_ACCOUNT,
            )


# ── Keychain integration ──────────────────────────────────────────────────────


class TestKeychainRetrieval:
    def test_keychain_success_yields_token(self, tmp_path):
        provider = _provider(tmp_path)
        token = provider()
        assert isinstance(token, str)
        assert token  # non-empty

    def test_keychain_failure_raises_token_error(self, tmp_path):
        pfx_path = tmp_path / "test.pfx"
        pfx_path.write_bytes(_TEST_PFX_BYTES)

        calls: list[str] = []

        def _failing_get_pw():
            calls.append("called")
            raise MicrosoftGraphCertTokenError("Keychain item not found or access denied.")

        provider = MicrosoftGraphCertTokenProvider(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            pfx_path=pfx_path,
            keychain_service=KEYCHAIN_SERVICE,
            keychain_account=KEYCHAIN_ACCOUNT,
            _http_post=lambda url, data: _make_fake_token_response(),
        )
        provider._get_pfx_password = _failing_get_pw  # type: ignore[method-assign]

        with pytest.raises(MicrosoftGraphCertTokenError):
            provider()
        assert calls == ["called"]

    def test_password_not_in_exception_message(self, tmp_path):
        pfx_path = tmp_path / "test.pfx"
        pfx_path.write_bytes(_TEST_PFX_BYTES)

        def _bad_pw():
            raise MicrosoftGraphCertTokenError("Keychain item not found or access denied.")

        provider = MicrosoftGraphCertTokenProvider(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            pfx_path=pfx_path,
            keychain_service=KEYCHAIN_SERVICE,
            keychain_account=KEYCHAIN_ACCOUNT,
            _http_post=lambda url, data: _make_fake_token_response(),
        )
        provider._get_pfx_password = _bad_pw  # type: ignore[method-assign]

        with pytest.raises(MicrosoftGraphCertTokenError) as exc_info:
            provider()

        msg = str(exc_info.value)
        assert "testpassword" not in msg
        assert _TEST_PFX_PASSWORD.decode() not in msg


# ── PFX loading and client assertion ─────────────────────────────────────────


class TestPfxAndAssertion:
    def test_pfx_loads_without_credential_store(self, tmp_path):
        """PFX is parsed in-memory; private_key is available but not stored."""
        provider = _provider(tmp_path)
        private_key, cert_der = provider._load_cert_material()
        assert private_key is not None
        assert isinstance(cert_der, bytes)
        assert len(cert_der) > 0

    def test_assertion_has_correct_claims(self, tmp_path):
        provider = _provider(tmp_path)
        private_key, cert_der = provider._load_cert_material()
        assertion = provider._build_assertion(private_key, cert_der)

        # Decode without verification to inspect claims.
        claims = jwt.decode(
            assertion,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["RS256"],
        )
        assert claims["iss"] == CLIENT_ID
        assert claims["sub"] == CLIENT_ID
        assert TENANT_ID in claims["aud"]
        assert "jti" in claims
        assert "nbf" in claims
        assert claims["exp"] > claims["nbf"]

    def test_assertion_x5t_header_is_correct(self, tmp_path):
        provider = _provider(tmp_path)
        private_key, cert_der = provider._load_cert_material()
        assertion = provider._build_assertion(private_key, cert_der)

        header = jwt.get_unverified_header(assertion)
        assert header["alg"] == "RS256"
        assert header["typ"] == "JWT"
        assert "x5t" in header

        # Verify x5t is the base64url SHA-1 thumbprint.
        expected_x5t = base64.urlsafe_b64encode(hashlib.sha1(cert_der).digest()).rstrip(b"=").decode()
        assert header["x5t"] == expected_x5t

    def test_malformed_pfx_raises_token_error(self, tmp_path):
        pfx_path = tmp_path / "bad.pfx"
        pfx_path.write_bytes(b"this is not a pfx")

        provider = MicrosoftGraphCertTokenProvider(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            pfx_path=pfx_path,
            keychain_service=KEYCHAIN_SERVICE,
            keychain_account=KEYCHAIN_ACCOUNT,
            _http_post=lambda url, data: _make_fake_token_response(),
        )
        provider._get_pfx_password = lambda: b"doesntmatter"  # type: ignore[method-assign]

        with pytest.raises(MicrosoftGraphCertTokenError, match="parse PFX"):
            provider()


# ── Graph audience and scope ──────────────────────────────────────────────────


class TestGraphAudienceAndScope:
    def test_graph_audience_is_verified(self, tmp_path):
        """A token with a wrong audience is rejected."""
        provider = _provider(
            tmp_path,
            http_post=lambda url, data: _make_fake_token_response(audience="https://wrong.example.com"),
        )
        with pytest.raises(MicrosoftGraphCertTokenError, match="audience"):
            provider()

    def test_graph_microsoft_com_audience_accepted(self, tmp_path):
        provider = _provider(
            tmp_path,
            http_post=lambda url, data: _make_fake_token_response(audience="https://graph.microsoft.com"),
        )
        token = provider()
        assert token

    def test_scope_in_request_body(self, tmp_path):
        captured: list[bytes] = []

        def _capture(url: str, data: bytes) -> bytes:
            captured.append(data)
            return _make_fake_token_response()

        provider = _provider(tmp_path, http_post=_capture)
        provider()

        assert captured
        body_str = captured[0].decode("ascii")
        assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body_str
        assert "client_assertion_type=" in body_str
        assert "grant_type=client_credentials" in body_str

    def test_no_client_secret_in_request_body(self, tmp_path):
        captured: list[bytes] = []

        def _capture(url: str, data: bytes) -> bytes:
            captured.append(data)
            return _make_fake_token_response()

        provider = _provider(tmp_path, http_post=_capture)
        provider()

        body_str = captured[0].decode("ascii")
        assert "client_secret" not in body_str


# ── Token caching, skew, and concurrent refresh ───────────────────────────────


class TestTokenCaching:
    def test_token_is_cached_and_reused(self, tmp_path):
        call_count = [0]

        def _counting_post(url: str, data: bytes) -> bytes:
            call_count[0] += 1
            return _make_fake_token_response(expires_in=3600)

        provider = _provider(tmp_path, http_post=_counting_post)
        token1 = provider()
        token2 = provider()

        assert call_count[0] == 1
        assert token1 == token2

    def test_stale_token_triggers_refresh(self, tmp_path):
        call_count = [0]
        tokens = ["token-A", "token-B"]

        def _counting_post(url: str, data: bytes) -> bytes:
            call_count[0] += 1
            idx = min(call_count[0] - 1, len(tokens) - 1)
            raw_token = jwt.encode(
                {"aud": "https://graph.microsoft.com", "exp": int(time.time()) + 3600},
                "secret",
                algorithm="HS256",
            )
            # Return with very short expiry so second call triggers refresh.
            expires = 1 if call_count[0] == 1 else 3600
            return json.dumps({"access_token": raw_token, "expires_in": expires}).encode()

        provider = _provider(tmp_path, http_post=_counting_post)
        provider.skew_seconds = 0  # type: ignore[attr-defined]
        provider._skew = 0

        token1 = provider()

        # Force the cached token to appear stale by backdating its expiry.
        if provider._cached is not None:
            provider._cached.expires_at = time.monotonic() - 1  # already expired

        token2 = provider()
        assert call_count[0] == 2

    def test_concurrent_calls_deduplicate_refresh(self, tmp_path):
        call_count = [0]
        barrier = threading.Barrier(5)

        def _slow_post(url: str, data: bytes) -> bytes:
            call_count[0] += 1
            time.sleep(0.05)
            return _make_fake_token_response(expires_in=3600)

        provider = _provider(tmp_path, http_post=_slow_post)
        tokens: list[str] = []
        errors: list[Exception] = []

        def _call():
            try:
                barrier.wait()
                tokens.append(provider())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert len(tokens) == 5
        # Only one HTTP call should have been made.
        assert call_count[0] == 1

    def test_skew_triggers_early_refresh(self, tmp_path):
        call_count = [0]

        def _post(url: str, data: bytes) -> bytes:
            call_count[0] += 1
            expires = 100 if call_count[0] == 1 else 3600
            raw_token = jwt.encode(
                {"aud": "https://graph.microsoft.com", "exp": int(time.time()) + expires},
                "secret", algorithm="HS256",
            )
            return json.dumps({"access_token": raw_token, "expires_in": expires}).encode()

        provider = _provider(tmp_path, http_post=_post)
        provider._skew = 200  # skew is larger than the first token's lifetime (100s)

        provider()
        # Token's remaining life (100s) < skew (200s) → next call should refresh.
        provider()

        assert call_count[0] == 2


# ── HTTP and token failures ───────────────────────────────────────────────────


class TestHttpAndTokenFailures:
    def test_http_error_raises_token_error(self, tmp_path):
        def _fail(url: str, data: bytes) -> bytes:
            raise OSError("Connection refused")

        provider = _provider(tmp_path, http_post=_fail)
        with pytest.raises(MicrosoftGraphCertTokenError, match="Token endpoint"):
            provider()

    def test_non_json_response_raises_token_error(self, tmp_path):
        provider = _provider(tmp_path, http_post=lambda url, data: b"not json")
        with pytest.raises(MicrosoftGraphCertTokenError, match="non-JSON"):
            provider()

    def test_missing_access_token_raises_token_error(self, tmp_path):
        provider = _provider(
            tmp_path,
            http_post=lambda url, data: json.dumps({"error": "invalid_client"}).encode(),
        )
        with pytest.raises(MicrosoftGraphCertTokenError, match="access_token"):
            provider()

    def test_sensitive_material_absent_from_token_error(self, tmp_path):
        provider = _provider(tmp_path, http_post=lambda url, data: b"bad json {{{{")
        with pytest.raises(MicrosoftGraphCertTokenError) as exc_info:
            provider()
        msg = str(exc_info.value)
        # Ensure no sensitive identifiers leak.
        assert CLIENT_ID not in msg
        assert TENANT_ID not in msg

    def test_no_client_secret_fallback(self, tmp_path):
        """Provider never falls back to env-var client-secret auth."""
        captured_bodies: list[str] = []

        def _capture(url: str, data: bytes) -> bytes:
            captured_bodies.append(data.decode())
            return _make_fake_token_response()

        provider = _provider(tmp_path, http_post=_capture)
        provider()

        for body in captured_bodies:
            assert "client_secret" not in body
            # Must use assertion flow, not client-secret.
            assert "client_assertion" in body
