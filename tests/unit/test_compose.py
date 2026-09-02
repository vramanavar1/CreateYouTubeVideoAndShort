"""Composition: the filter graph, the duration budget, and a real render."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from ytshort.config import Settings
from ytshort.integrations.audio import AudioSource, NoAudioAvailable
from ytshort.integrations.ffmpeg import FFmpeg, MediaProbe
from ytshort.pipeline.signals import RetryableFailure
from ytshort.stages.compose import FPS, IMAGE_BODY_SECONDS, ComposeStage
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.stages.safety import SafetyStage
from ytshort.stages.thumbnail import ThumbnailStage


class StubFFmpeg:
    """Records the command instead of running it."""

    def __init__(self, duration: float = 4.0, has_audio: bool = False) -> None:
        self.ffmpeg = "ffmpeg"
        self.ffprobe = "ffprobe"
        self.available = True
        self.commands: list[list[str]] = []
        self._duration = duration
        self._has_audio = has_audio

    def require(self) -> None:
        return None

    def probe(self, path: Path) -> MediaProbe:
        return MediaProbe(
            duration_seconds=self._duration,
            width=720,
            height=1280,
            has_audio=self._has_audio,
            video_stream_count=1,
            audio_stream_count=1 if self._has_audio else 0,
            format_name="mov,mp4",
        )

    def run(self, args: list[str], *, description: str = "") -> None:
        self.commands.append(args)
        # Pretend the render happened so the stage can record the artefact.
        Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42")


def _prepared_job(ctx, gmail, png_bytes, *, with_video: bool = False):
    files = {"pic.png": png_bytes}
    if with_video:
        files["clip.mp4"] = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    gmail.add_message("m1", subject="Sunset over the lake", files=files)
    job = discover_jobs(ctx)[0]
    IngestStage().run(job, ctx)
    if not with_video:
        SafetyStage().run(job, ctx)
    else:
        job.media.primary_image = job.attachments[0].stored_path
        job.media.primary_video = job.attachments[1].stored_path
    ThumbnailStage().run(job, ctx)
    return job


class TestFilterGraph:
    def _command(self, ctx, gmail, png_bytes, audio_track, **kwargs) -> list[str]:
        job = _prepared_job(ctx, gmail, png_bytes, with_video=kwargs.pop("with_video", False))
        stub = StubFFmpeg(**kwargs)
        ComposeStage()._build_command  # noqa: B018 - documents the seam under test

        stage = ComposeStage()
        original = FFmpeg.from_settings
        try:
            FFmpeg.from_settings = classmethod(lambda cls, settings: stub)  # type: ignore[assignment]
            stage.run(job, ctx)
        finally:
            FFmpeg.from_settings = original  # type: ignore[assignment]

        assert stub.commands
        return stub.commands[0]

    def test_concatenates_exactly_three_normalised_segments(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        command = self._command(ctx, gmail, png_bytes, audio_track)
        graph = command[command.index("-filter_complex") + 1]

        # Bumper, body, bumper -- each normalised to identical parameters before
        # concat, because concat rejects inputs whose parameters differ.
        assert graph.count("concat=n=3:v=1:a=0") == 1
        assert "[v0][v1][v2]concat" in graph
        # Three segments, each ending in identical fps/pixel-format settings.
        assert graph.count(f"fps={FPS},format=yuv420p") == 3

    def test_the_body_gets_a_blurred_backdrop_not_black_bars(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        # The bumpers already sit on a blurred backdrop; plain black bars on the
        # body would read as a rendering fault.
        command = self._command(ctx, gmail, png_bytes, audio_track)
        graph = command[command.index("-filter_complex") + 1]

        assert "gblur" in graph
        # The foreground is still contain-fitted -- the source is never cropped.
        assert "[v1fg]scale=1080:1920:force_original_aspect_ratio=decrease" in graph

    def test_music_is_trimmed_faded_and_ducked(self, ctx, gmail, png_bytes, audio_track) -> None:
        command = self._command(ctx, gmail, png_bytes, audio_track)
        graph = command[command.index("-filter_complex") + 1]

        assert "atrim=0:" in graph
        assert f"volume={ctx.settings.background_audio_gain}" in graph
        assert "afade=t=in" in graph and "afade=t=out" in graph
        # -stream_loop -1 keeps a short track long enough for any Short.
        assert "-stream_loop" in command

    def test_clip_audio_is_delayed_past_the_bumper_and_mixed(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        command = self._command(ctx, gmail, png_bytes, audio_track, with_video=True, has_audio=True)
        graph = command[command.index("-filter_complex") + 1]

        delay_ms = int(ctx.settings.bumper_seconds * 1000)
        assert f"adelay={delay_ms}|{delay_ms}" in graph
        # normalize=0 preserves the gains we set rather than halving both.
        assert "amix=inputs=2:duration=first:normalize=0" in graph

    def test_a_silent_clip_uses_music_alone(self, ctx, gmail, png_bytes, audio_track) -> None:
        command = self._command(
            ctx, gmail, png_bytes, audio_track, with_video=True, has_audio=False
        )
        graph = command[command.index("-filter_complex") + 1]

        assert "amix" not in graph
        assert "[music]anull[aout]" in graph

    def test_output_strips_metadata_and_is_stream_ready(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        command = self._command(ctx, gmail, png_bytes, audio_track)

        assert "-map_metadata" in command
        assert command[command.index("-map_metadata") + 1] == "-1"
        assert "+faststart" in command
        assert command[command.index("-pix_fmt") + 1] == "yuv420p"


class TestDurationBudget:
    def _run_with(self, ctx, gmail, png_bytes, duration: float):
        job = _prepared_job(ctx, gmail, png_bytes, with_video=True)
        stub = StubFFmpeg(duration=duration)
        original = FFmpeg.from_settings
        try:
            FFmpeg.from_settings = classmethod(lambda cls, settings: stub)  # type: ignore[assignment]
            ComposeStage().run(job, ctx)
        finally:
            FFmpeg.from_settings = original  # type: ignore[assignment]
        return job

    def test_a_short_clip_is_kept_whole(self, ctx, gmail, png_bytes, audio_track) -> None:
        job = self._run_with(ctx, gmail, png_bytes, duration=5.0)
        expected = 5.0 + 2 * ctx.settings.bumper_seconds
        assert job.media.duration_seconds == pytest.approx(expected)

    def test_a_long_clip_is_trimmed_to_the_shorts_ceiling(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        job = self._run_with(ctx, gmail, png_bytes, duration=600.0)

        assert job.media.duration_seconds == pytest.approx(ctx.settings.max_short_seconds)
        finding = next(f for f in job.findings if f.kind == "compose.body_trimmed")
        assert "600.0s" in finding.detail

    def test_an_image_only_email_holds_the_still(self, ctx, gmail, png_bytes, audio_track) -> None:
        job = _prepared_job(ctx, gmail, png_bytes)
        stub = StubFFmpeg()
        original = FFmpeg.from_settings
        try:
            FFmpeg.from_settings = classmethod(lambda cls, settings: stub)  # type: ignore[assignment]
            ComposeStage().run(job, ctx)
        finally:
            FFmpeg.from_settings = original  # type: ignore[assignment]

        expected = IMAGE_BODY_SECONDS + 2 * ctx.settings.bumper_seconds
        assert job.media.duration_seconds == pytest.approx(expected)


class TestPreconditions:
    def test_missing_audio_is_retryable_not_fatal(self, ctx, gmail, png_bytes) -> None:
        job = _prepared_job(ctx, gmail, png_bytes)  # no audio_track fixture used

        stub = StubFFmpeg()
        original = FFmpeg.from_settings
        try:
            FFmpeg.from_settings = classmethod(lambda cls, settings: stub)  # type: ignore[assignment]
            with pytest.raises(RetryableFailure, match="licensed audio"):
                ComposeStage().run(job, ctx)
        finally:
            FFmpeg.from_settings = original  # type: ignore[assignment]


class TestAudioSource:
    def test_selection_is_stable_per_job(self, settings) -> None:
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            (settings.audio_dir / name).write_bytes(b"x")
        source = AudioSource(settings.audio_dir)

        assert source.select("abc123") == source.select("abc123")

    def test_different_jobs_can_get_different_tracks(self, settings) -> None:
        for name in ("a.mp3", "b.mp3", "c.mp3", "d.mp3"):
            (settings.audio_dir / name).write_bytes(b"x")
        source = AudioSource(settings.audio_dir)

        picks = {source.select(f"{index:032x}").name for index in range(16)}
        assert len(picks) > 1

    def test_empty_folder_raises_with_an_actionable_message(self, settings) -> None:
        with pytest.raises(NoAudioAvailable, match="rights to use"):
            AudioSource(settings.audio_dir).select("abc")

    def test_non_audio_files_are_ignored(self, settings) -> None:
        (settings.audio_dir / "notes.txt").write_bytes(b"x")
        assert AudioSource(settings.audio_dir).tracks() == []


@requires_ffmpeg
@pytest.mark.ffmpeg
class TestRealRender:
    def test_renders_a_playable_vertical_short(
        self, ctx, gmail, png_bytes, audio_track, sample_video
    ) -> None:
        gmail.add_message(
            "m1",
            subject="Sunset over the lake",
            files={"pic.png": png_bytes, "clip.mp4": sample_video.read_bytes()},
        )
        job = discover_jobs(ctx)[0]
        IngestStage().run(job, ctx)
        SafetyStage().run(job, ctx)
        ThumbnailStage().run(job, ctx)

        ComposeStage().run(job, ctx)

        output = ctx.media_store.resolve(job.job_id, job.media.composed_video)
        probe = FFmpeg.from_settings(ctx.settings).probe(output)

        assert (probe.width, probe.height) == (1080, 1920)
        assert probe.has_audio
        # 2s clip + two bumpers, within a frame's tolerance.
        expected = 2.0 + 2 * ctx.settings.bumper_seconds
        assert probe.duration_seconds == pytest.approx(expected, abs=0.3)

    def test_an_image_only_email_still_renders(
        self, ctx, gmail, png_bytes, audio_track
    ) -> None:
        gmail.add_message("m1", subject="A still", files={"pic.png": png_bytes})
        job = discover_jobs(ctx)[0]
        IngestStage().run(job, ctx)
        SafetyStage().run(job, ctx)
        ThumbnailStage().run(job, ctx)

        ComposeStage().run(job, ctx)

        output = ctx.media_store.resolve(job.job_id, job.media.composed_video)
        probe = FFmpeg.from_settings(ctx.settings).probe(output)
        assert probe.height > probe.width
        assert probe.has_audio  # the background track


def test_settings_reload_is_isolated(settings: Settings) -> None:
    """Guards the fixture itself: tests must never read the developer's .env."""
    assert settings.data_dir.name == "var"
    assert "tmp" in str(settings.data_dir).lower() or settings.data_dir.is_absolute()
