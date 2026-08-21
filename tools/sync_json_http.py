"""A minimal synchronous JSON-over-HTTP transport with a mockable seam.

The audited write clients (:mod:`tools.msgraph_write_client`,
:mod:`tools.ghl_client`) are synchronous on purpose: the log-then-write ordering
they enforce is easier to read, and easier to *prove* in a test, when it is
straight-line code.  They take their transport as a plain callable, so a test can
pass a spy and assert the destination API was invoked exactly zero times.

Nothing here knows about auditing.  Everything here runs *after* the audit gate.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: Statuses worth another attempt.  429 and 5xx are transient by definition; 408
#: is a server-side read timeout.
DEFAULT_RETRY_STATUSES: tuple[int, ...] = (408, 429, 500, 502, 503, 504)


@dataclass(frozen=True)
class HttpResponse:
    """A completed HTTP exchange, including non-2xx ones (no raising in transport)."""

    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON, or return ``None`` for an empty/unparseable body."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


class HttpRequestError(RuntimeError):
    """A non-2xx response that is not worth (or has run out of) retries."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        detail = body.strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {detail}")


#: ``(method, url, *, headers, json_body, timeout) -> HttpResponse``
RequestFn = Callable[..., HttpResponse]


def urllib_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
) -> HttpResponse:
    """Perform one HTTP request with the stdlib and return the response as data.

    Non-2xx responses come back as :class:`HttpResponse`, not exceptions, so the
    retry layer above can decide what to do with them.
    """
    data = None
    request_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status_code=response.status,
                body=response.read(),
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status_code=exc.code,
            body=exc.read(),
            headers=dict(exc.headers.items()) if exc.headers else {},
        )


def request_json(
    request_fn: RequestFn,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES,
) -> Any:
    """Call *request_fn* with retries and return the parsed JSON body.

    Raises :class:`HttpRequestError` when the final attempt is non-2xx.  Retries
    happen entirely inside this function — the audit gate sits above it, so a
    blocked write never reaches even the first attempt.
    """
    attempts = max(0, int(max_retries)) + 1
    last: HttpResponse | None = None
    for attempt in range(attempts):
        response = request_fn(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )
        last = response
        if 200 <= response.status_code < 300:
            return response.json()
        if response.status_code not in retry_statuses or attempt == attempts - 1:
            break
        sleep(_retry_delay(response, attempt))

    assert last is not None  # attempts >= 1
    raise HttpRequestError(method, url, last.status_code, last.text)


def _retry_delay(response: HttpResponse, attempt: int) -> float:
    """Honour ``Retry-After`` when present, else exponential backoff."""
    raw = ""
    for key, value in (response.headers or {}).items():
        if key.lower() == "retry-after":
            raw = str(value).strip()
            break
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(2**attempt)


def build_url(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> str:
    """Join *base_url* and *path*, appending non-``None`` *params* as a query string."""
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    filtered = {k: v for k, v in (params or {}).items() if v is not None}
    if filtered:
        url = f"{url}?{urllib.parse.urlencode(filtered)}"
    return url
