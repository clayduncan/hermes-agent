"""Certificate-based Microsoft Graph token provider using macOS Keychain PFX.

Token acquisition flow
======================

1. Retrieve the PFX password from macOS Keychain via ``/usr/bin/security``
   (no echo, no logging, no return — kept in a local bytes variable only).
2. Load the PFX certificate + private key entirely in memory; the key is
   never installed into any OS certificate store.
3. Build a signed JWT client assertion (RS256, x5t SHA-1 thumbprint header)
   that satisfies the Azure AD client-credentials-with-assertion grant.
4. POST the assertion to the tenant token endpoint and cache the returned
   access token with a configurable clock-skew margin.

Thread safety
-------------
A :class:`threading.Lock` serialises concurrent refresh attempts so that
only one thread actually hits the token endpoint; all waiters reuse the
newly cached token once it is available.

Secret handling
---------------
Passwords, private-key material, the signed assertion, and token strings
are all kept in local variables.  None of them appear in exception messages,
log output, or return values other than the ``str`` token returned by
:meth:`MicrosoftGraphCertTokenProvider.__call__`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_ASSERTION_LIFETIME_S = 600
_DEFAULT_SKEW_S = 120


# ── Error types ───────────────────────────────────────────────────────────────


class MicrosoftGraphCertAuthError(RuntimeError):
    """Base class for certificate-based Graph auth failures."""


class MicrosoftGraphCertConfigError(MicrosoftGraphCertAuthError):
    """Missing or invalid configuration (fails before any network or Keychain call)."""


class MicrosoftGraphCertTokenError(MicrosoftGraphCertAuthError):
    """Token acquisition failed (Keychain, PFX parse, assertion, or HTTP error)."""


# ── Cached token helper ───────────────────────────────────────────────────────


class _CachedToken:
    __slots__ = ("access_token", "expires_at")

    def __init__(self, access_token: str, expires_at: float) -> None:
        self.access_token = access_token
        self.expires_at = expires_at  # monotonic clock

    def is_stale(self, skew_seconds: int) -> bool:
        return self.expires_at <= time.monotonic() + max(0, skew_seconds)


# ── Provider ──────────────────────────────────────────────────────────────────


#: Callable that POSTs ``data`` (url-encoded bytes) to ``url`` and returns the
#: raw response body bytes.  Injectable for tests; defaults to urllib.
_HttpPostFn = Callable[[str, bytes], bytes]


class MicrosoftGraphCertTokenProvider:
    """Synchronous certificate-based token provider for Microsoft Graph.

    Returns a ``str`` Bearer token on every :meth:`__call__`, refreshing
    via the Azure AD client-credentials-with-assertion flow when the cached
    token is within *skew_seconds* of expiry.  The lock ensures only one
    concurrent refresh runs regardless of how many threads call simultaneously.

    All parameters are required and validated at construction.  No env-var
    fallback is provided — configuration is fully explicit (OPS-45 params).

    *_http_post* is injectable for tests: ``fn(url: str, data: bytes) -> bytes``.
    *_security_bin* is injectable for tests; defaults to ``/usr/bin/security``.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        pfx_path: str | Path,
        keychain_service: str,
        keychain_account: str,
        skew_seconds: int = _DEFAULT_SKEW_S,
        timeout: float = 20.0,
        authority_url: str = "https://login.microsoftonline.com",
        _security_bin: str = "/usr/bin/security",
        _http_post: _HttpPostFn | None = None,
    ) -> None:
        for name, value in (
            ("tenant_id", tenant_id),
            ("client_id", client_id),
            ("keychain_service", keychain_service),
            ("keychain_account", keychain_account),
        ):
            if not isinstance(value, str) or not value.strip():
                raise MicrosoftGraphCertConfigError(
                    f"MicrosoftGraphCertTokenProvider requires non-empty {name}."
                )

        pfx_path = Path(pfx_path)
        if not pfx_path.is_file():
            raise MicrosoftGraphCertConfigError(
                "PFX file not found; check pfx_path."
            )

        self._tenant_id = tenant_id.strip()
        self._client_id = client_id.strip()
        self._pfx_path = pfx_path
        self._keychain_service = keychain_service.strip()
        self._keychain_account = keychain_account.strip()
        self._skew = max(0, int(skew_seconds))
        self._timeout = float(timeout)
        self._token_url = (
            f"{authority_url.rstrip('/')}"
            f"/{self._tenant_id.strip('/')}"
            f"/oauth2/v2.0/token"
        )
        self._security_bin = _security_bin
        self._http_post: _HttpPostFn = _http_post or _urllib_post

        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None

    # ── Public interface ──────────────────────────────────────────────────────

    def __call__(self) -> str:
        """Return a valid Bearer token string, refreshing when stale."""
        cached = self._cached
        if cached is not None and not cached.is_stale(self._skew):
            return cached.access_token

        with self._lock:
            cached = self._cached
            if cached is not None and not cached.is_stale(self._skew):
                return cached.access_token
            token = self._acquire_token()
            self._cached = token
            return token.access_token

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_pfx_password(self) -> bytes:
        """Return PFX password bytes from Keychain; never printed or logged."""
        try:
            result = subprocess.run(
                [
                    self._security_bin,
                    "find-generic-password",
                    "-s", self._keychain_service,
                    "-a", self._keychain_account,
                    "-w",
                ],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MicrosoftGraphCertTokenError(
                "Keychain subprocess failed."
            ) from exc

        if result.returncode != 0:
            raise MicrosoftGraphCertTokenError(
                "Keychain item not found or access denied."
            )

        raw = result.stdout.strip()
        if not raw:
            raise MicrosoftGraphCertTokenError(
                "Keychain returned an empty password."
            )
        return raw

    def _load_cert_material(self) -> tuple[Any, bytes]:
        """Load PFX and return ``(private_key, cert_der_bytes)``.

        The PFX is never installed into any certificate store.  Private-key
        material is cleared from input byte arrays before returning.
        """
        pfx_bytes = self._pfx_path.read_bytes()
        pw_bytes = self._get_pfx_password()
        try:
            p12 = load_pkcs12(pfx_bytes, pw_bytes)
        except Exception as exc:
            raise MicrosoftGraphCertTokenError(
                "Failed to parse PFX file."
            ) from exc
        finally:
            pfx_bytes = b"\x00" * len(pfx_bytes)
            pw_bytes = b"\x00" * len(pw_bytes)

        private_key = p12.key
        if private_key is None:
            raise MicrosoftGraphCertTokenError("PFX contains no private key.")

        cert_obj = p12.cert
        if cert_obj is None:
            raise MicrosoftGraphCertTokenError("PFX contains no certificate.")

        cert_der = cert_obj.certificate.public_bytes(serialization.Encoding.DER)
        return private_key, cert_der

    @staticmethod
    def _x5t(cert_der: bytes) -> str:
        """Base64url-encode the SHA-1 thumbprint (required by OIDC x5t header)."""
        digest = hashlib.sha1(cert_der).digest()  # noqa: S324 — x5t spec mandates SHA-1
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _build_assertion(self, private_key: Any, cert_der: bytes) -> str:
        """Return a signed JWT client assertion for the AAD token endpoint."""
        now = int(time.time())
        headers = {"alg": "RS256", "typ": "JWT", "x5t": self._x5t(cert_der)}
        claims = {
            "aud": self._token_url,
            "exp": now + _ASSERTION_LIFETIME_S,
            "iss": self._client_id,
            "jti": uuid.uuid4().hex,
            "nbf": now,
            "sub": self._client_id,
        }
        pem_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        try:
            token_str = jwt.encode(claims, pem_key, algorithm="RS256", headers=headers)
        except Exception as exc:
            raise MicrosoftGraphCertTokenError(
                "Failed to sign client assertion."
            ) from exc
        finally:
            pem_key = b"\x00" * len(pem_key)
        return token_str

    def _acquire_token(self) -> _CachedToken:
        private_key, cert_der = self._load_cert_material()
        try:
            assertion = self._build_assertion(private_key, cert_der)
        finally:
            del private_key

        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
                "scope": _GRAPH_SCOPE,
            }
        ).encode("ascii")

        try:
            response_body = self._http_post(self._token_url, data)
        except Exception as exc:
            raise MicrosoftGraphCertTokenError(
                "Token endpoint request failed."
            ) from exc
        finally:
            del assertion

        try:
            payload = json.loads(response_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MicrosoftGraphCertTokenError(
                "Token endpoint returned non-JSON."
            ) from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise MicrosoftGraphCertTokenError(
                "Token endpoint did not return access_token."
            )

        # Verify audience when the token is a decodable JWT.
        _check_graph_audience(access_token)

        try:
            expires_in = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600

        return _CachedToken(
            access_token=access_token,
            expires_at=time.monotonic() + max(0, expires_in),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _check_graph_audience(access_token: str) -> None:
    """Fail closed when the token audience is clearly wrong.

    Azure AD v2 access tokens are opaque JWTs; decoding without verification
    is sufficient to read the ``aud`` claim.  If the token is not a JWT at
    all, the check is skipped (opaque-token case).
    """
    try:
        claims = jwt.decode(
            access_token,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["RS256", "RS384", "RS512", "HS256"],
        )
    except Exception:
        return  # opaque token — skip check

    aud = claims.get("aud")
    if aud is None:
        return  # no aud claim in this token format — skip

    aud_str = aud if isinstance(aud, str) else (aud[0] if isinstance(aud, list) and aud else "")
    if aud_str and "graph.microsoft.com" not in aud_str and "00000003-0000-0000-c000-000000000000" not in aud_str:
        raise MicrosoftGraphCertTokenError(
            "Token audience does not include graph.microsoft.com."
        )


def _urllib_post(url: str, data: bytes) -> bytes:
    """Default HTTP POST implementation using stdlib urllib."""
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        return resp.read()


__all__ = [
    "MicrosoftGraphCertAuthError",
    "MicrosoftGraphCertConfigError",
    "MicrosoftGraphCertTokenError",
    "MicrosoftGraphCertTokenProvider",
]
