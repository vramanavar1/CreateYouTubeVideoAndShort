"""Which faults earn a retry.

The default has to be "no". Retrying a permanent error is not caution -- a 403 for
a revoked credential answers the same way forever, and asking again on every
scheduled run is a self-inflicted DoS with a bill attached.
"""

from __future__ import annotations

import pytest

from ytshort.integrations.faults import http_status, is_transient


class _Resp:
    """Shaped like googleapiclient's HttpError.resp."""

    def __init__(self, status: int | str) -> None:
        self.status = status


class _GoogleHttpError(Exception):
    def __init__(self, status: int | str) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = _Resp(status)


class _UrllibHttpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class TestHttpStatus:
    def test_it_reads_a_google_style_error(self) -> None:
        assert http_status(_GoogleHttpError(503)) == 503

    def test_it_reads_a_urllib_style_error(self) -> None:
        assert http_status(_UrllibHttpError(429)) == 429

    def test_it_reads_a_string_status(self) -> None:
        # httplib2 hands back a dict-like response whose status is a string.
        assert http_status(_GoogleHttpError("500")) == 500

    def test_it_returns_none_without_a_status(self) -> None:
        assert http_status(ValueError("not http")) is None


class TestIsTransient:
    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_transient_statuses_retry(self, status: int) -> None:
        assert is_transient(_GoogleHttpError(status)) is True

    @pytest.mark.parametrize(
        ("status", "why"),
        [
            (400, "malformed request"),
            (401, "credential expired or revoked"),
            (403, "quota exhausted or channel unverified"),
            (404, "the message was deleted"),
            (413, "too large"),
        ],
    )
    def test_permanent_statuses_do_not_retry(self, status: int, why: str) -> None:
        assert is_transient(_GoogleHttpError(status)) is False, why

    @pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionError("reset")])
    def test_network_faults_without_a_status_retry(self, exc: Exception) -> None:
        assert is_transient(exc) is True

    @pytest.mark.parametrize("exc", [ValueError("bug"), KeyError("bug"), TypeError("bug")])
    def test_unrecognised_exceptions_are_permanent(self, exc: Exception) -> None:
        # A bug in our own code retried on a timer is a bug you learn about from
        # the bill instead of the logs.
        assert is_transient(exc) is False
