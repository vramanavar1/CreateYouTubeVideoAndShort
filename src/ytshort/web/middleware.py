"""ASGI middleware for the review UI: correlation ids and security headers.

Both are written as raw ASGI rather than ``BaseHTTPMiddleware``. The latter runs
the endpoint in a separate anyio task; ContextVar propagation happens to work,
but the failure mode is subtle and version-dependent, and a raw class costs about
fifteen more lines.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ccol import new_correlation_id, use_correlation
from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "x-correlation-id"
TRACEPARENT_HEADER = "traceparent"

#: An inbound correlation id is untrusted text that lands in a log field. Without
#: this bound, "\r\nlevel: INFO ..." forges log lines and a 100 KB value is a
#: cheap way to inflate log volume. Reject and mint our own instead.
_SAFE_ID = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_TRACE_ID = re.compile(r"\A[0-9a-f]{32}\Z")

#: Only these need to load: one inline <style> block, inline style attributes,
#: and media served from /media. The templates contain no scripts at all, so
#: script-src inherits 'none' from default-src.
_CSP = (
    "default-src 'none'; "
    "img-src 'self'; "
    "media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": _CSP,
}


def _header(scope: Scope, name: str) -> str:
    wanted = name.encode("latin-1")
    for key, value in scope.get("headers", ()):
        if key.lower() == wanted:
            return value.decode("latin-1", errors="replace")
    return ""


def _inbound_correlation_id(scope: Scope) -> str:
    candidate = _header(scope, CORRELATION_HEADER).strip()
    if _SAFE_ID.match(candidate):
        return candidate

    # A W3C traceparent is "00-<32 hex trace id>-<16 hex span id>-<flags>".
    parts = _header(scope, TRACEPARENT_HEADER).strip().split("-")
    if len(parts) == 4 and _TRACE_ID.match(parts[1]):
        return parts[1]
    return ""


class CorrelationIdMiddleware:
    """Binds a correlation id for the duration of every request.

    ``/health`` is skipped: the readiness probe hits it every ten seconds, which
    is 8,640 billed telemetry records a day saying nothing. The response body is
    untouched, so the endpoint stays empty as it must.
    """

    def __init__(
        self, app: ASGIApp, *, skip_paths: frozenset[str] = frozenset({"/health"})
    ) -> None:
        self.app = app
        self.skip_paths = skip_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.skip_paths:
            await self.app(scope, receive, send)
            return

        cid = _inbound_correlation_id(scope) or new_correlation_id()

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_HEADER] = cid
            await send(message)

        with use_correlation(cid):
            await self.app(scope, receive, send_with_id)


class SecurityHeadersMiddleware:
    """Adds the response headers the review UI would otherwise be missing.

    Defence in depth behind Jinja's autoescaping: the UI renders
    attacker-influenced text -- email subjects, filenames, OCR output -- into HTML.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
