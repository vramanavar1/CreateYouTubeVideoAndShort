"""Readiness checks that `ytshort doctor` surfaces."""

from __future__ import annotations

import pytest

from ytshort.config import Settings

_MANIFEST = """# Audio licences

| File | Source | Licence | Attribution required? |
|---|---|---|---|
| calm-loop.mp3 | YouTube Audio Library | CC BY 4.0 | Yes |
"""


@pytest.fixture
def audio_settings(tmp_path, monkeypatch):
    """Settings pointed at an empty, writable audio directory."""
    audio = tmp_path / "audio"
    audio.mkdir()
    monkeypatch.setenv("YTSHORT_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("YTSHORT_AUDIO_DIR", str(audio))
    monkeypatch.setenv("YTSHORT_ALLOWED_SENDERS", "sender@example.com")

    def build() -> Settings:
        return Settings.load(env_file=tmp_path / "absent.env", strict=False)

    return audio, build


def _problem_text(settings: Settings) -> str:
    return "\n".join(settings.validation_problems())


class TestAudioLicenceManifest:
    """docs/youtube-audit.md tells Google this manifest is authoritative.

    Nothing used to check that a dropped track was actually recorded in it, so an
    operator could publish with an unlicensed MP3 while the audit submission
    claimed otherwise.
    """

    def test_an_unlisted_track_is_reported(self, audio_settings) -> None:
        audio, build = audio_settings
        (audio / "AUDIO_LICENSES.md").write_text(_MANIFEST, encoding="utf-8")
        (audio / "mystery.mp3").write_bytes(b"ID3")

        problems = _problem_text(build())
        assert "mystery.mp3" in problems
        assert "no row" in problems

    def test_a_listed_track_is_accepted(self, audio_settings) -> None:
        audio, build = audio_settings
        (audio / "AUDIO_LICENSES.md").write_text(_MANIFEST, encoding="utf-8")
        (audio / "calm-loop.mp3").write_bytes(b"ID3")

        problems = _problem_text(build())
        assert "calm-loop.mp3" not in problems
        assert "No licensed audio track found" not in problems

    def test_a_missing_manifest_is_reported_when_tracks_exist(
        self, audio_settings
    ) -> None:
        audio, build = audio_settings
        (audio / "calm-loop.mp3").write_bytes(b"ID3")

        assert "manifest missing" in _problem_text(build())

    def test_an_empty_directory_reports_only_the_missing_track(
        self, audio_settings
    ) -> None:
        # The repo ships assets/audio/ empty on purpose; that should read as "drop
        # a track in", not as a licensing complaint.
        _audio, build = audio_settings
        problems = _problem_text(build())

        assert "No licensed audio track found" in problems
        assert "manifest missing" not in problems

    def test_non_audio_files_are_ignored(self, audio_settings) -> None:
        audio, build = audio_settings
        (audio / "AUDIO_LICENSES.md").write_text(_MANIFEST, encoding="utf-8")
        (audio / "calm-loop.mp3").write_bytes(b"ID3")
        (audio / "notes.txt").write_text("scratch", encoding="utf-8")

        assert "notes.txt" not in _problem_text(build())


class TestTelemetryReadiness:
    def test_a_connection_string_without_the_extra_is_reported(
        self, audio_settings, monkeypatch
    ) -> None:
        # The realistic deployment slip: Bicep sets the env var, the image was
        # built without the extra. The app still runs, but doctor should say so.
        _audio, build = audio_settings
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")

        settings = build()
        problems = _problem_text(settings)
        assert settings.telemetry_configured is True
        assert "observability" in problems

    def test_nothing_is_reported_when_telemetry_is_unconfigured(
        self, audio_settings
    ) -> None:
        _audio, build = audio_settings
        assert "observability" not in _problem_text(build())
