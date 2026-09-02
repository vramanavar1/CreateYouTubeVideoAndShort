"""Render the Short: thumbnail bumper -> body -> thumbnail bumper, over music.

One ffmpeg invocation does the whole thing. The filter graph normalises every
segment to 1080x1920 / 30 fps / yuv420p *before* concatenating -- the concat
filter requires identical parameters on all inputs, and a phone clip almost never
matches a rendered still. Using the concat demuxer instead (the common shortcut)
fails or silently produces garbage on mismatched inputs.

Audio: the licensed background track runs the full length, ducked to
``YTSHORT_BACKGROUND_AUDIO_GAIN`` and faded at both ends. If the body clip has its
own audio it is delayed past the opening bumper and mixed on top at full volume,
so speech stays intelligible over the music.
"""

from __future__ import annotations

from pathlib import Path

from ytshort.contracts.models import Finding, Job, JobState, Severity
from ytshort.integrations.audio import AudioSource, NoAudioAvailable
from ytshort.integrations.ffmpeg import FFmpeg, FFmpegError, FFmpegNotAvailable
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)

WIDTH, HEIGHT = 1080, 1920
FPS = 30
#: How long a still image is held when the email carried no video.
IMAGE_BODY_SECONDS = 6.0
#: YouTube treats <=180 s vertical uploads as Shorts; beyond that it is a normal video.
SHORTS_CEILING_SECONDS = 180
#: Anything past this reads as a long-form video to the algorithm even if allowed.
SHORTS_SWEET_SPOT_SECONDS = 60
FADE_SECONDS = 1.0

#: Normalisation for the bumpers, which are already rendered at exactly 1080x1920.
_NORMALISE = (
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
    f"setsar=1,fps={FPS},format=yuv420p"
)


def _blurred_pad(stream: str, label: str) -> str:
    """Fit a segment into the 9:16 frame over a blurred copy of itself.

    A landscape clip has to be letterboxed somehow, and plain black bars next to
    bumpers that already use a blurred backdrop look like a rendering bug. This
    matches the two, and never crops the source -- the foreground is still a
    contain-fit.
    """
    return (
        f"[{stream}]split=2[{label}bg][{label}fg];"
        f"[{label}bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},gblur=sigma=40,eq=brightness=-0.2[{label}bgb];"
        f"[{label}fg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease[{label}fgs];"
        f"[{label}bgb][{label}fgs]overlay=(W-w)/2:(H-h)/2,"
        f"setsar=1,fps={FPS},format=yuv420p[{label}]"
    )


class ComposeStage(BaseStage):
    name = "compose"
    success_state = JobState.composed

    def run(self, job: Job, ctx: PipelineContext) -> None:
        settings = ctx.settings
        ffmpeg = FFmpeg.from_settings(settings)
        try:
            ffmpeg.require()
        except FFmpegNotAvailable as exc:
            # Retryable, not fatal: install ffmpeg and re-run the same job.
            raise RetryableFailure(str(exc)) from exc

        if job.media.thumbnail_tall is None:
            raise HaltPipeline("compose needs a rendered thumbnail bumper")

        job_dir = ctx.media_store.job_dir(job.job_id)
        bumper = ctx.media_store.resolve(job.job_id, job.media.thumbnail_tall)

        try:
            track = AudioSource(settings.audio_dir).select(job.job_id)
        except NoAudioAvailable as exc:
            raise RetryableFailure(str(exc)) from exc

        bumper_seconds = max(0.5, settings.bumper_seconds)
        body_path, body_seconds, body_has_audio = self._resolve_body(job, ctx, ffmpeg)

        ceiling = min(settings.max_short_seconds, SHORTS_CEILING_SECONDS)
        budget = ceiling - 2 * bumper_seconds
        if budget <= 0.5:
            raise HaltPipeline(
                f"bumpers ({2 * bumper_seconds}s) leave no room inside the {ceiling}s limit"
            )

        if body_seconds > budget:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="compose.body_trimmed",
                    severity=Severity.warn,
                    where=body_path.name,
                    detail=(
                        f"source runs {body_seconds:.1f}s; trimmed to {budget:.1f}s to stay "
                        f"inside the {ceiling}s Shorts limit"
                    ),
                    action_taken="trimmed",
                )
            )
            body_seconds = budget

        total = round(bumper_seconds * 2 + body_seconds, 3)
        output = job_dir / "short.mp4"

        args = self._build_command(
            bumper=bumper,
            body=body_path,
            body_is_video=job.media.primary_video is not None,
            body_seconds=body_seconds,
            body_has_audio=body_has_audio,
            bumper_seconds=bumper_seconds,
            track=track,
            total=total,
            gain=settings.background_audio_gain,
            output=output,
        )

        try:
            ffmpeg.run(args, description="compose short")
        except FFmpegError as exc:
            raise RetryableFailure(f"render failed: {exc}") from exc

        job.media.composed_video = output.name
        job.media.audio_track = track.name
        job.media.duration_seconds = total

        if total > SHORTS_SWEET_SPOT_SECONDS:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="compose.long_short",
                    severity=Severity.info,
                    where=output.name,
                    detail=(
                        f"the Short runs {total:.1f}s; under {SHORTS_SWEET_SPOT_SECONDS}s "
                        "generally performs better"
                    ),
                )
            )

        log.info(
            "short composed",
            extra={"file": output.name, "seconds": total, "track": track.name},
        )

    # -- helpers -----------------------------------------------------------
    def _resolve_body(
        self, job: Job, ctx: PipelineContext, ffmpeg: FFmpeg
    ) -> tuple[Path, float, bool]:
        """The middle segment: the video if there is one, else the still image."""
        if job.media.primary_video:
            path = ctx.media_store.resolve(job.job_id, job.media.primary_video)
            try:
                probe = ffmpeg.probe(path)
            except FFmpegError as exc:
                raise HaltPipeline(f"body video could not be probed: {exc}") from exc
            return path, probe.duration_seconds, probe.has_audio

        if job.media.primary_image:
            path = ctx.media_store.resolve(job.job_id, job.media.primary_image)
            return path, IMAGE_BODY_SECONDS, False

        raise HaltPipeline("nothing to compose: neither video nor image is available")

    def _build_command(
        self,
        *,
        bumper: Path,
        body: Path,
        body_is_video: bool,
        body_seconds: float,
        body_has_audio: bool,
        bumper_seconds: float,
        track: Path,
        total: float,
        gain: float,
        output: Path,
    ) -> list[str]:
        args: list[str] = [
            # 0: opening bumper
            "-loop", "1", "-t", f"{bumper_seconds}", "-i", str(bumper),
        ]
        # 1: body
        if body_is_video:
            args += ["-t", f"{body_seconds}", "-i", str(body)]
        else:
            args += ["-loop", "1", "-t", f"{body_seconds}", "-i", str(body)]
        args += [
            # 2: closing bumper (same file, second decode -- cheap for a still)
            "-loop", "1", "-t", f"{bumper_seconds}", "-i", str(bumper),
            # 3: background music, looped so atrim below always has enough
            "-stream_loop", "-1", "-i", str(track),
        ]

        fade_out_start = max(0.0, total - FADE_SECONDS)
        filters = [
            f"[0:v]{_NORMALISE}[v0]",
            # The body is the only segment whose aspect ratio is unknown, so it
            # is the only one that needs the blurred backdrop treatment.
            _blurred_pad("1:v", "v1"),
            f"[2:v]{_NORMALISE}[v2]",
            "[v0][v1][v2]concat=n=3:v=1:a=0[vout]",
            (
                f"[3:a]atrim=0:{total},asetpts=N/SR/TB,volume={gain},"
                f"afade=t=in:st=0:d={FADE_SECONDS},"
                f"afade=t=out:st={fade_out_start}:d={FADE_SECONDS}[music]"
            ),
        ]

        if body_has_audio:
            delay_ms = int(bumper_seconds * 1000)
            filters += [
                # Delay the clip's own audio past the opening bumper, then pad so
                # amix does not cut the music short when the clip ends.
                f"[1:a]adelay={delay_ms}|{delay_ms},apad,atrim=0:{total},"
                "asetpts=N/SR/TB[speech]",
                # normalize=0 keeps the gains we set instead of amix halving both.
                "[music][speech]amix=inputs=2:duration=first:normalize=0[aout]",
            ]
        else:
            filters.append("[music]anull[aout]")

        args += [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-map", "[aout]",
            "-t", f"{total}",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            # Strip all container metadata -- source device, location, timestamps.
            "-map_metadata", "-1",
            # Put the moov atom first so the review UI can stream the preview.
            "-movflags", "+faststart",
            str(output),
        ]
        return args
