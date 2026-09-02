"""The VirusTotal hash-lookup scanner used in the Linux container.

The behaviour that matters most here is the honest reporting of *unknown*: a file
nobody has ever submitted to VirusTotal has not been screened, and saying so is
the difference between the reviewer knowing what they are approving and not.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from ytshort.integrations.scanner import (
    NoopScanner,
    VirusTotalScanner,
    build_scanner,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


def _stats(**kwargs) -> dict:
    base = {"harmless": 60, "malicious": 0, "suspicious": 0, "undetected": 12}
    base.update(kwargs)
    return {"data": {"attributes": {"last_analysis_stats": base}}}


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "pic.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
    return path


class TestVerdicts:
    def test_clean_when_no_engine_flags_it(self, sample, monkeypatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: _Response(_stats())
        )

        result = VirusTotalScanner("key").scan(sample)

        assert result.clean is True
        assert result.skipped is False
        assert "72 engines" in result.detail

    def test_malicious_detection_blocks(self, sample, monkeypatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: _Response(_stats(malicious=7))
        )

        result = VirusTotalScanner("key").scan(sample)

        assert result.clean is False
        assert "7 engines" in result.detail

    def test_suspicious_alone_also_blocks(self, sample, monkeypatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: _Response(_stats(suspicious=2))
        )

        assert VirusTotalScanner("key").scan(sample).clean is False


class TestHonestUnknowns:
    def _http_error(self, code: int):
        def raise_it(*args, **kwargs):
            raise urllib.error.HTTPError("url", code, "err", {}, BytesIO(b""))

        return raise_it

    def test_an_unknown_hash_is_reported_as_not_screened(self, sample, monkeypatch) -> None:
        # 404 means nobody has ever submitted this file. That is NOT clean.
        monkeypatch.setattr("urllib.request.urlopen", self._http_error(404))

        result = VirusTotalScanner("key").scan(sample)

        assert result.skipped is True
        assert "unknown" in result.detail.lower()

    def test_rate_limiting_is_not_a_clean_verdict(self, sample, monkeypatch) -> None:
        monkeypatch.setattr("urllib.request.urlopen", self._http_error(429))

        result = VirusTotalScanner("key").scan(sample)

        assert result.skipped is True
        assert "429" in result.detail

    def test_network_failure_is_not_a_clean_verdict(self, sample, monkeypatch) -> None:
        def unreachable(*args, **kwargs):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr("urllib.request.urlopen", unreachable)

        result = VirusTotalScanner("key").scan(sample)

        assert result.skipped is True
        assert "unreachable" in result.detail

    def test_a_missing_api_key_is_reported_not_ignored(self, sample) -> None:
        result = VirusTotalScanner("").scan(sample)

        assert result.skipped is True
        assert "VIRUSTOTAL_API_KEY" in result.detail


class TestPrivacy:
    def test_only_the_hash_is_sent_never_the_file(self, sample, monkeypatch) -> None:
        # The whole reason for choosing a hash lookup: someone's private photo
        # must never leave the system.
        captured = {}

        def capture(request, *args, **kwargs):
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["headers"] = dict(request.headers)
            return _Response(_stats())

        monkeypatch.setattr("urllib.request.urlopen", capture)
        VirusTotalScanner("secret-key").scan(sample)

        assert captured["data"] is None  # no body, so no upload
        assert sample.read_bytes() not in (captured["data"] or b"")
        # The URL carries a 64-char hex digest and nothing else identifying.
        digest = captured["url"].rsplit("/", 1)[-1]
        assert len(digest) == 64
        assert "pic.png" not in captured["url"]

    def test_the_api_key_travels_in_a_header_not_the_url(self, sample, monkeypatch) -> None:
        captured = {}

        def capture(request, *args, **kwargs):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return _Response(_stats())

        monkeypatch.setattr("urllib.request.urlopen", capture)
        VirusTotalScanner("secret-key").scan(sample)

        assert "secret-key" not in captured["url"]  # would land in proxy logs
        assert captured["headers"]["X-apikey".lower()] == "secret-key"


class TestSelection:
    def test_virustotal_is_selectable(self) -> None:
        assert isinstance(build_scanner("virustotal", "key"), VirusTotalScanner)

    def test_unknown_provider_falls_back_to_noop(self) -> None:
        assert isinstance(build_scanner("clamav-maybe"), NoopScanner)

    def test_noop_reports_itself_as_skipped(self, sample) -> None:
        # A disabled scanner must never look like a clean scan.
        result = NoopScanner().scan(sample)
        assert result.skipped is True
