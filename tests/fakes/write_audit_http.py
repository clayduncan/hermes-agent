"""A spy HTTP transport for testing the audited write clients.

The point of this fake is the assertion it makes possible: ``spy.writes == []``.
Checking the destination's final state cannot prove the audit line landed first —
a mutation that never happened and a mutation that was rolled back look identical
from the outside.  Counting invocations of the transport can tell them apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from tools.sync_json_http import HttpResponse

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Any = None

    @property
    def is_mutating(self) -> bool:
        return self.method.upper() in MUTATING_METHODS


def json_response(payload: Any, status_code: int = 200) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status_code=status_code, body=body, headers={})


class SpyTransport:
    """Routes requests by ``(method, path)`` and records every call.

    Register responses with :meth:`route`.  Several responses for one route are
    consumed in order, with the final one repeating — which is how a test gives
    different answers to the pre-write ``GET`` and the post-write read-back of the
    same URL.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.calls: list[RecordedRequest] = []
        self._routes: dict[tuple[str, str], list[Any]] = {}

    # ── setup ────────────────────────────────────────────────────────────────

    def route(self, method: str, path: str, *responses: Any) -> "SpyTransport":
        """Queue one or more responses for ``method path``.

        Each response may be a plain object (sent as a 200 JSON body), an
        :class:`HttpResponse` (for non-2xx), or an exception instance (raised).
        """
        key = (method.upper(), path)
        self._routes.setdefault(key, []).extend(responses)
        return self

    def force_mutating_responses(self, response: Any) -> "SpyTransport":
        """Make every already-routed mutating endpoint answer with *response*.

        Used to prove the audit gate blocks the *first* attempt: wire the writes to
        a retryable status, and any leak past the gate shows up as recorded calls.
        """
        for key in list(self._routes):
            if key[0] in MUTATING_METHODS:
                self._routes[key] = [response]
        return self

    # ── assertions ───────────────────────────────────────────────────────────

    @property
    def writes(self) -> list[RecordedRequest]:
        """Every mutating call that reached the destination API."""
        return [call for call in self.calls if call.is_mutating]

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(call.method, self._path_of(call.url)) for call in self.calls]

    # ── transport ────────────────────────────────────────────────────────────

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self.calls.append(
            RecordedRequest(
                method=method.upper(),
                url=url,
                headers=dict(headers or {}),
                json_body=json_body,
            )
        )
        key = (method.upper(), self._path_of(url))
        queued = self._routes.get(key)
        if not queued:
            raise AssertionError(
                f"SpyTransport got an unrouted request: {method.upper()} {url}. "
                f"Routed: {sorted(self._routes)}"
            )
        response = queued[0] if len(queued) == 1 else queued.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, HttpResponse):
            return response
        return json_response(response)

    def _path_of(self, url: str) -> str:
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        return path.split("?", 1)[0]
