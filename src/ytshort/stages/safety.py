"""Security screening: is this file what it claims to be, and is it safe to publish?

Layered, cheapest-first, each layer recording a Finding rather than silently
passing or silently failing:

1. magic bytes vs the declared type      -- catches ``payload.exe`` renamed ``photo.png``
2. structural sanity                     -- decompression/dimension bombs, absurd stream counts
3. metadata scrub                        -- EXIF/GPS is stripped from stored images
4. malware scan                          -- pluggable provider (Windows Defender by default)
5. image moderation                      -- pluggable provider (off by default)

Any ``blocking`` finding halts the job into ``quarantined``. It never reaches the
reviewer as approvable, because "click approve on the malware" is not a decision
a human should be offered.
"""

from __future__ import annotations

from pathlib import Path

import filetype
from PIL import Image, UnidentifiedImageError

from ytshort.contracts.models import Attachment, Finding, Job, JobState, Severity
from ytshort.integrations.ffmpeg import FFmpeg, FFmpegError, FFmpegNotAvailable
from ytshort.integrations.moderation import build_moderator
from ytshort.integrations.scanner import build_scanner
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)

# ~50 MP. Above this a "photo" is almost certainly a decompression bomb rather
# than something a phone produced. Pillow's own default warns at 89 MP.
MAX_IMAGE_PIXELS = 50_000_000

# A legitimate phone clip has one video track and at most a couple of audio
# tracks. Dozens of streams is a container designed to break a decoder.
MAX_STREAMS = 6

#: Extension -> the magic-byte types that are acceptable for it. HEIC is absent
#: on purpose: ``filetype`` reports it as ``heic``/``heif`` inconsistently across
#: versions, so it is handled by the explicit alias set below.
_EXPECTED: dict[str, set[str]] = {
    ".jpg": {"jpg", "jpeg"},
    ".jpeg": {"jpg", "jpeg"},
    ".png": {"png"},
    ".webp": {"webp"},
    ".heic": {"heic", "heif"},
    ".mp4": {"mp4", "m4v", "mov"},  # ISO-BMFF brands overlap; mov is a valid read
    ".mov": {"mov", "mp4"},
}


class SafetyStage(BaseStage):
    name = "safety"
    success_state = JobState.screened

    def run(self, job: Job, ctx: PipelineContext) -> None:
        settings = ctx.settings
        scanner = ctx.scanner or build_scanner(
            settings.malware_scanner, settings.virustotal_api_key
        )
        moderator = ctx.moderator or build_moderator(
            settings.moderation_provider, settings.anthropic_api_key
        )
        ffmpeg = FFmpeg.from_settings(settings)

        for attachment in job.media_attachments:
            if attachment.stored_path is None:
                continue
            path = ctx.media_store.resolve(job.job_id, attachment.stored_path)

            self._check_magic_bytes(job, attachment, path)
            if not attachment.accepted:
                continue

            if attachment.kind == "image":
                self._check_image(job, attachment, path, ctx)
            else:
                self._check_video(job, attachment, path, ffmpeg)

            if not attachment.accepted:
                continue

            self._scan(job, attachment, path, scanner)

            if attachment.kind == "image" and attachment.accepted:
                self._moderate(job, attachment, path, moderator)

        if job.blocking_findings:
            reasons = "; ".join(f.detail for f in job.blocking_findings)
            raise HaltPipeline(f"blocked by safety screening: {reasons}")

        # Rejecting individual attachments can leave nothing usable behind.
        if not job.media_attachments:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="media.none_survived_screening",
                    severity=Severity.blocking,
                    where="email",
                    detail="every attachment was rejected during screening",
                    action_taken="halted",
                )
            )
            raise HaltPipeline("no attachment survived screening")

        self._repoint_primaries(job)

    # -- layer 1: magic bytes ---------------------------------------------
    def _check_magic_bytes(self, job: Job, attachment: Attachment, path: Path) -> None:
        guess = filetype.guess(str(path))
        detected = guess.extension if guess else None
        attachment.detected_mime = guess.mime if guess else None

        suffix = Path(attachment.filename).suffix.lower()
        expected = _EXPECTED.get(suffix, set())

        if detected is None:
            # Not a recognisable media container at all. This is the renamed-.exe
            # case and the corrupt-download case; both must stop here.
            self._reject(
                job,
                attachment,
                kind="content.unrecognised",
                detail=(
                    f"{attachment.filename}: content is not a recognised image or video "
                    "container despite its extension"
                ),
                severity=Severity.blocking,
            )
            return

        if detected not in expected:
            self._reject(
                job,
                attachment,
                kind="content.type_mismatch",
                detail=(
                    f"{attachment.filename}: extension says {suffix} but the file's magic "
                    f"bytes say {detected}"
                ),
                severity=Severity.blocking,
            )

    # -- layer 2/3: structure and metadata --------------------------------
    def _check_image(
        self, job: Job, attachment: Attachment, path: Path, ctx: PipelineContext
    ) -> None:
        try:
            with Image.open(path) as image:
                width, height = image.size
                pixels = width * height
                if pixels > MAX_IMAGE_PIXELS:
                    self._reject(
                        job,
                        attachment,
                        kind="image.too_many_pixels",
                        detail=(
                            f"{attachment.filename}: {width}x{height} = {pixels} pixels "
                            f"exceeds the {MAX_IMAGE_PIXELS}-pixel guard"
                        ),
                        severity=Severity.blocking,
                    )
                    return

                exif = image.getexif()
                has_gps = bool(exif.get_ifd(0x8825)) if exif else False
                has_exif = bool(exif)
                # Decode now, inside the guard, so a bomb fails here rather than
                # somewhere less careful downstream.
                image.load()
                cleaned = image.convert("RGB") if image.mode not in ("RGB", "L") else image.copy()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            self._reject(
                job,
                attachment,
                kind="image.unreadable",
                detail=f"{attachment.filename}: image could not be decoded ({exc})",
                severity=Severity.blocking,
            )
            return

        # Re-save without metadata. Pillow writes no EXIF unless asked to, so a
        # plain save is the scrub. The cleaned copy replaces the original as the
        # attachment's stored file, so nothing downstream can pick up the GPS.
        clean_path = path.with_name(f"{path.stem}_clean.png")
        cleaned.save(clean_path, format="PNG")
        cleaned.close()
        attachment.stored_path = clean_path.name

        if has_gps or has_exif:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="image.metadata_stripped",
                    severity=Severity.warn if has_gps else Severity.info,
                    where=attachment.filename,
                    detail=(
                        "image carried GPS coordinates in EXIF"
                        if has_gps
                        else "image carried EXIF metadata"
                    ),
                    action_taken="metadata removed from the stored copy",
                )
            )

    def _check_video(
        self, job: Job, attachment: Attachment, path: Path, ffmpeg: FFmpeg
    ) -> None:
        try:
            probe = ffmpeg.probe(path)
        except FFmpegNotAvailable:
            # Cannot inspect the container. Say so rather than assume it is fine.
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="video.not_screened",
                    severity=Severity.warn,
                    where=attachment.filename,
                    detail="ffprobe is unavailable, so the video container was not inspected",
                    action_taken="screening skipped",
                )
            )
            return
        except FFmpegError as exc:
            self._reject(
                job,
                attachment,
                kind="video.unreadable",
                detail=f"{attachment.filename}: ffprobe could not read the file ({exc})",
                severity=Severity.blocking,
            )
            return

        streams = probe.video_stream_count + probe.audio_stream_count
        if streams > MAX_STREAMS:
            self._reject(
                job,
                attachment,
                kind="video.too_many_streams",
                detail=f"{attachment.filename}: {streams} streams exceeds the {MAX_STREAMS} guard",
                severity=Severity.blocking,
            )
            return

        if probe.duration_seconds <= 0:
            self._reject(
                job,
                attachment,
                kind="video.zero_duration",
                detail=f"{attachment.filename}: reported duration is {probe.duration_seconds}s",
                severity=Severity.blocking,
            )
            return

        if not probe.is_vertical:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="video.not_vertical",
                    severity=Severity.info,
                    where=attachment.filename,
                    detail=(
                        f"source is {probe.width}x{probe.height} (landscape); it will be "
                        "letterboxed into the 1080x1920 Short frame"
                    ),
                    action_taken="will be padded during compose",
                )
            )

    # -- layer 4: malware --------------------------------------------------
    def _scan(self, job: Job, attachment: Attachment, path: Path, scanner) -> None:
        result = scanner.scan(path)

        if not result.clean:
            self._reject(
                job,
                attachment,
                kind="malware.detected",
                detail=f"{attachment.filename}: {result.provider} reported {result.detail}",
                severity=Severity.blocking,
            )
            return

        if result.skipped:
            # Not clean -- unscreened. The distinction matters to the reviewer.
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="malware.not_scanned",
                    severity=Severity.warn,
                    where=attachment.filename,
                    detail=result.detail,
                    action_taken="screening skipped",
                )
            )
        else:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="malware.clean",
                    severity=Severity.info,
                    where=attachment.filename,
                    detail=f"{result.provider}: {result.detail}",
                )
            )

    # -- layer 5: moderation ----------------------------------------------
    def _moderate(self, job: Job, attachment: Attachment, path: Path, moderator) -> None:
        stored = attachment.stored_path or path.name
        target = path.with_name(stored)
        result = moderator.moderate(target)

        if result.skipped:
            if moderator.name != "none":
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="moderation.not_screened",
                        severity=Severity.warn,
                        where=attachment.filename,
                        detail=result.detail,
                        action_taken="screening skipped",
                    )
                )
            return

        if result.flagged:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="moderation.flagged",
                    severity=Severity.blocking,
                    where=attachment.filename,
                    detail=(
                        f"{', '.join(result.categories) or 'declined to analyse'}: {result.detail}"
                    ),
                    action_taken="quarantined",
                )
            )
        else:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="moderation.clear",
                    severity=Severity.info,
                    where=attachment.filename,
                    detail=result.detail,
                )
            )

    # -- helpers -----------------------------------------------------------
    def _reject(
        self,
        job: Job,
        attachment: Attachment,
        *,
        kind: str,
        detail: str,
        severity: Severity,
    ) -> None:
        attachment.accepted = False
        attachment.reject_reason = detail
        job.add_finding(
            Finding(
                stage=self.name,
                kind=kind,
                severity=severity,
                where=attachment.filename,
                detail=detail,
                action_taken="attachment rejected",
            )
        )
        log.warning("attachment rejected", extra={"file": attachment.filename, "kind": kind})

    def _repoint_primaries(self, job: Job) -> None:
        """Screening rewrites image paths and may drop attachments -- refresh."""
        job.media.primary_image = None
        job.media.primary_video = None
        for attachment in job.media_attachments:
            if attachment.kind == "image" and job.media.primary_image is None:
                job.media.primary_image = attachment.stored_path
            elif attachment.kind == "video" and job.media.primary_video is None:
                job.media.primary_video = attachment.stored_path
