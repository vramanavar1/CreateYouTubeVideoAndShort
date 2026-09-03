"""Readiness checks that `ytshort doctor` surfaces."""

from __future__ import annotations

import pytest

from ytshort import config
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


    def test_a_name_mentioned_only_in_prose_does_not_count(self, audio_settings) -> None:
        # The bug this catches: a whole-document substring match let any passing
        # mention satisfy the check -- including a heading saying to delete the
        # file. A control you can satisfy by naming the problem is no control.
        audio, build = audio_settings
        (audio / "AUDIO_LICENSES.md").write_text(
            _MANIFEST + "\n## Action required: remove mystery.mp3\n", encoding="utf-8"
        )
        (audio / "mystery.mp3").write_bytes(b"ID3")

        assert "mystery.mp3" in _problem_text(build())

    def test_a_row_in_any_table_counts(self, audio_settings) -> None:
        audio, build = audio_settings
        (audio / "AUDIO_LICENSES.md").write_text(_MANIFEST, encoding="utf-8")
        (audio / "calm-loop.mp3").write_bytes(b"ID3")

        assert "no row" not in _problem_text(build())


class TestTelemetryReadiness:
    """Whether the extra is installed is patched, never inferred.

    These assertions must not depend on what happens to be in the developer's
    virtualenv -- the optional extra may or may not be present, and a test that
    flips with it tells you nothing.
    """

    def test_a_connection_string_without_the_extra_is_reported(
        self, audio_settings, monkeypatch
    ) -> None:
        # The realistic deployment slip: Bicep sets the env var, the image was
        # built without the extra. The app still runs, but doctor should say so.
        _audio, build = audio_settings
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
        monkeypatch.setattr(config, "_module_available", lambda _name: False)

        settings = build()
        assert settings.telemetry_configured is True
        assert "observability" in _problem_text(settings)

    def test_nothing_is_reported_when_the_extra_is_installed(
        self, audio_settings, monkeypatch
    ) -> None:
        _audio, build = audio_settings
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
        monkeypatch.setattr(config, "_module_available", lambda _name: True)

        assert "observability" not in _problem_text(build())

    def test_nothing_is_reported_when_telemetry_is_unconfigured(
        self, audio_settings, monkeypatch
    ) -> None:
        _audio, build = audio_settings
        monkeypatch.setattr(config, "_module_available", lambda _name: False)

        assert "observability" not in _problem_text(build())

    def test_a_missing_parent_package_does_not_raise(self) -> None:
        # find_spec imports parent packages on the way down, so asking for
        # "azure.monitor.opentelemetry" without the azure extra raises instead of
        # returning None -- which would take out `ytshort doctor`, the one command
        # you run when things are already broken.
        assert config._module_available("definitely_not_installed.sub.module") is False


class TestArtDirectorReadiness:
    def test_foundry_without_an_endpoint_is_reported(self, audio_settings, monkeypatch) -> None:
        _audio, build = audio_settings
        monkeypatch.setenv("YTSHORT_ART_DIRECTOR", "foundry")

        problems = _problem_text(build())
        assert "YTSHORT_FOUNDRY_ENDPOINT" in problems

    def test_foundry_without_the_extra_is_reported(self, audio_settings, monkeypatch) -> None:
        _audio, build = audio_settings
        monkeypatch.setenv("YTSHORT_ART_DIRECTOR", "foundry")
        monkeypatch.setenv("YTSHORT_FOUNDRY_ENDPOINT", "https://x.openai.azure.com/openai/v1/")
        monkeypatch.setattr(config, "_module_available", lambda _name: False)

        assert "--extra foundry" in _problem_text(build())

    def test_nothing_is_reported_when_the_director_is_off(self, audio_settings) -> None:
        _audio, build = audio_settings

        problems = _problem_text(build())
        assert "FOUNDRY" not in problems

    def test_the_deployment_name_defaults_to_mini(self, audio_settings) -> None:
        _audio, build = audio_settings

        assert build().foundry_deployment == "gpt-4o-mini"
