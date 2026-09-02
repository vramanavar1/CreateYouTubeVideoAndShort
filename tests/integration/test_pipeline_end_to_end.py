"""The whole pipeline, inbox to short URL, entirely offline.

These are the tests that would catch a wiring mistake between stages -- the kind
no unit test sees because each stage passes on its own.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import make_png, requires_ffmpeg
from tests.fakes import FakeScanner
from ytshort.config import Settings
from ytshort.contracts.models import JobState, Severity
from ytshort.integrations.ffmpeg import FFmpeg
from ytshort.pipeline.runner import PipelineRunner
from ytshort.runtime import record_decision, run_job
from ytshort.stages import build_stages, discover_jobs

pytestmark = pytest.mark.integration


def _drive(ctx, gmail, *, files: dict[str, bytes], subject: str = "Sunset over the lake"):
    gmail.add_message("m1", subject=subject, files=files)
    jobs = discover_jobs(ctx)
    assert len(jobs) == 1
    return run_job(jobs[0], ctx)


@requires_ffmpeg
@pytest.mark.ffmpeg
class TestHappyPath:
    def test_image_email_reaches_review_then_publishes(
        self, ctx, gmail, youtube, png_bytes, audio_track, monkeypatch
    ) -> None:
        monkeypatch.setenv("YTSHORT_SINKS", "file,email")
        monkeypatch.setenv("YTSHORT_EMAIL_RECIPIENTS", "me@example.com")
        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()

        outcome = _drive(ctx, gmail, files={"pic.png": png_bytes})

        # --- parks for a human, publishes nothing ---
        job = outcome.job
        assert job.state is JobState.awaiting_review
        assert outcome.stages_run == ["ingest", "safety", "pii", "thumbnail", "compose"]
        assert youtube.uploads == []
        assert job.media.composed_video == "short.mp4"
        assert ctx.media_store.resolve(job.job_id, "short.mp4").exists()

        # --- a human approves ---
        record_decision(job, ctx, decision="approved", reviewer="tester")
        final = run_job(job, ctx).job

        assert final.state is JobState.done
        assert len(youtube.uploads) == 1
        assert final.publication is not None
        assert final.publication.short_url == f"https://youtu.be/{final.publication.video_id}"

        # --- both sinks delivered ---
        assert {d.sink: d.ok for d in final.deliveries} == {"file": True, "email": True}
        assert len(gmail.sent) == 1
        assert final.publication.short_url in gmail.sent[0]["body_text"]

        record = json.loads(
            (ctx.settings.out_dir / f"{final.job_id}.json").read_text(encoding="utf-8")
        )
        assert record["short_url"] == final.publication.short_url

    def test_video_email_produces_a_vertical_short(
        self, ctx, gmail, youtube, png_bytes, audio_track, sample_video
    ) -> None:
        outcome = _drive(
            ctx,
            gmail,
            files={"pic.png": png_bytes, "clip.mp4": sample_video.read_bytes()},
        )
        job = outcome.job
        assert job.state is JobState.awaiting_review

        probe = FFmpeg.from_settings(ctx.settings).probe(
            ctx.media_store.resolve(job.job_id, job.media.composed_video)
        )
        assert (probe.width, probe.height) == (1080, 1920)
        assert probe.has_audio

    def test_the_whole_run_is_idempotent(
        self, ctx, gmail, youtube, png_bytes, audio_track
    ) -> None:
        outcome = _drive(ctx, gmail, files={"pic.png": png_bytes})
        job = outcome.job
        record_decision(job, ctx, decision="approved")
        run_job(job, ctx)

        # Re-running everything: no second job, no second upload, no second email.
        assert discover_jobs(ctx) == []
        again = run_job(ctx.job_store.load(job.job_id), ctx)

        assert len(youtube.uploads) == 1
        assert again.stages_run == []
        assert again.job.state is JobState.done


@requires_ffmpeg
@pytest.mark.ffmpeg
class TestRejectionPaths:
    def test_a_rejected_job_publishes_nothing(
        self, ctx, gmail, youtube, png_bytes, audio_track
    ) -> None:
        job = _drive(ctx, gmail, files={"pic.png": png_bytes}).job
        record_decision(job, ctx, decision="rejected", reason="wrong photo")

        final = run_job(job, ctx).job

        assert final.state is JobState.rejected
        assert youtube.uploads == []
        assert final.deliveries == []

    def test_a_warning_still_reaches_the_reviewer(
        self, ctx, gmail, youtube, png_bytes, audio_track
    ) -> None:
        # A phone number in the subject would be burned into the thumbnail.
        job = _drive(
            ctx, gmail, files={"pic.png": png_bytes}, subject="Call me on +44 7700 900123"
        ).job

        assert job.state is JobState.awaiting_review
        assert any(f.kind == "pii.phone" for f in job.warn_findings)


class TestBlockedPaths:
    """These stop before compose, so they need no ffmpeg."""

    def test_malware_quarantines_before_any_rendering(
        self, ctx, gmail, youtube, png_bytes
    ) -> None:
        ctx.scanner = FakeScanner(clean=False, detail="Trojan:Win32/Test")

        outcome = _drive(ctx, gmail, files={"pic.png": png_bytes})

        assert outcome.job.state is JobState.quarantined
        assert outcome.stopped_by == "safety"
        assert outcome.job.media.composed_video is None
        assert youtube.uploads == []

    def test_a_disguised_executable_quarantines(self, ctx, gmail, youtube) -> None:
        payload = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200 + b"PE\x00\x00"

        outcome = _drive(ctx, gmail, files={"holiday.png": payload})

        assert outcome.job.state is JobState.quarantined
        assert youtube.uploads == []

    def test_an_oversize_email_is_refused_at_ingest(self, ctx, gmail, youtube) -> None:
        limit = ctx.settings.max_total_attachment_bytes
        gmail.add_message(
            "m1", files={"big.png": make_png(64, 64)}, declared_sizes={"big.png": limit + 1}
        )
        job = discover_jobs(ctx)[0]

        outcome = PipelineRunner(build_stages(), ctx).run(job)

        assert outcome.job.state is JobState.quarantined
        assert outcome.stopped_by == "ingest"
        assert any(f.kind == "attachments.too_large" for f in outcome.job.blocking_findings)

    def test_blocking_pii_never_reaches_the_reviewer(
        self, ctx, gmail, youtube, png_bytes, monkeypatch
    ) -> None:
        monkeypatch.setenv("YTSHORT_PII_POLICY", "block")
        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()

        outcome = _drive(
            ctx, gmail, files={"pic.png": png_bytes}, subject="Card 4111 1111 1111 1111"
        )

        assert outcome.job.state is JobState.quarantined
        assert outcome.job.state is not JobState.awaiting_review
        assert any(f.severity is Severity.blocking for f in outcome.job.findings)

    def test_missing_audio_leaves_the_job_resumable(self, ctx, gmail, png_bytes) -> None:
        # No audio_track fixture: compose cannot run, but nothing is lost.
        outcome = _drive(ctx, gmail, files={"pic.png": png_bytes})

        assert outcome.stopped_by == "compose"
        assert outcome.job.state is JobState.screened  # not failed
        assert not outcome.job.stage_completed("compose")
        assert outcome.job.media.thumbnail_tall is not None  # earlier work kept
