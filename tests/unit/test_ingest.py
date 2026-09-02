"""Discovery guardrails and attachment download."""

from __future__ import annotations

import pytest

from ytshort.contracts.models import JobState, Severity, make_job_id
from ytshort.integrations.ffmpeg import FFmpeg, FFmpegNotAvailable
from ytshort.pipeline.runner import PipelineRunner
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.storage.counters import DailyCounter


class TestDiscovery:
    def test_creates_one_job_per_new_message(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", subject="First", files={"a.png": png_bytes})
        gmail.add_message("m2", subject="Second", files={"b.png": png_bytes})

        jobs = discover_jobs(ctx)

        assert len(jobs) == 2
        assert {j.source.subject for j in jobs} == {"First", "Second"}

    def test_the_same_message_never_produces_a_second_job(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", files={"a.png": png_bytes})

        assert len(discover_jobs(ctx)) == 1
        assert discover_jobs(ctx) == []  # idempotent

    def test_daily_cap_is_enforced(self, ctx, gmail, png_bytes, monkeypatch) -> None:
        monkeypatch.setenv("YTSHORT_MAX_EMAILS_PER_DAY", "2")
        from ytshort.config import Settings

        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()

        for index in range(5):
            gmail.add_message(f"m{index}", files={"a.png": png_bytes})

        assert len(discover_jobs(ctx)) == 2
        assert DailyCounter(ctx.settings.counters_dir).count() == 2
        assert discover_jobs(ctx) == []  # budget exhausted for the day

    def test_limit_argument_caps_a_single_run(self, ctx, gmail, png_bytes) -> None:
        for index in range(4):
            gmail.add_message(f"m{index}", files={"a.png": png_bytes})

        assert len(discover_jobs(ctx, limit=1)) == 1

    def test_sender_allow_list_filters(self, ctx, gmail, png_bytes, monkeypatch) -> None:
        monkeypatch.setenv("YTSHORT_ALLOWED_SENDERS", "trusted@example.com")
        from ytshort.config import Settings

        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()

        gmail.add_message("m1", sender="Trusted <trusted@example.com>", files={"a.png": png_bytes})
        gmail.add_message("m2", sender="stranger@example.com", files={"b.png": png_bytes})

        jobs = discover_jobs(ctx)

        assert len(jobs) == 1
        assert "trusted@example.com" in jobs[0].source.sender

    def test_messages_without_attachments_are_ignored(self, ctx, gmail) -> None:
        gmail.add_message("m1", files={})
        assert discover_jobs(ctx) == []

    def test_rejected_messages_do_not_consume_the_daily_budget(
        self, ctx, gmail, png_bytes, monkeypatch
    ) -> None:
        monkeypatch.setenv("YTSHORT_ALLOWED_SENDERS", "trusted@example.com")
        from ytshort.config import Settings

        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()

        gmail.add_message("m1", sender="stranger@example.com", files={"a.png": png_bytes})
        gmail.add_message("m2", sender="trusted@example.com", files={"b.png": png_bytes})

        discover_jobs(ctx)

        assert DailyCounter(ctx.settings.counters_dir).count() == 1


class TestIngestStage:
    def _run(self, ctx, message_id: str = "m1"):
        job = discover_jobs(ctx)[0]
        assert job.source.message_id == message_id
        return job

    def test_downloads_and_classifies_attachments(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", subject="Sunset", files={"pic.png": png_bytes})
        job = self._run(ctx)

        IngestStage().run(job, ctx)

        assert len(job.attachments) == 1
        assert job.attachments[0].kind == "image"
        assert job.attachments[0].sha256
        assert job.media.primary_image == "pic.png"
        assert job.title == "Sunset"

    def test_disallowed_types_are_skipped_not_downloaded(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message(
            "m1", files={"pic.png": png_bytes, "invoice.pdf": b"%PDF-1.4", "run.exe": b"MZ"}
        )
        job = self._run(ctx)

        IngestStage().run(job, ctx)

        rejected = [a for a in job.attachments if not a.accepted]
        assert {a.filename for a in rejected} == {"invoice.pdf", "run.exe"}
        assert all(a.stored_path is None for a in rejected)

    def test_oversize_batch_is_rejected_before_download(self, ctx, gmail, png_bytes) -> None:
        limit = ctx.settings.max_total_attachment_bytes
        gmail.add_message(
            "m1", files={"big.png": png_bytes}, declared_sizes={"big.png": limit + 1}
        )
        job = self._run(ctx)

        with pytest.raises(HaltPipeline, match="exceed"):
            IngestStage().run(job, ctx)

        assert any(f.kind == "attachments.too_large" for f in job.blocking_findings)
        # Nothing was fetched -- the declared size was enough to refuse.
        assert all(a.stored_path is None for a in job.attachments)

    def test_email_with_no_usable_media_halts(self, ctx, gmail) -> None:
        gmail.add_message("m1", files={"notes.txt": b"hello"})
        job = self._run(ctx)

        with pytest.raises(HaltPipeline, match="no usable image or video"):
            IngestStage().run(job, ctx)

        assert any(f.severity is Severity.blocking for f in job.findings)

    def test_download_failure_is_retryable(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = self._run(ctx)
        gmail.download_error = TimeoutError("connection reset")

        with pytest.raises(RetryableFailure, match="could not download"):
            IngestStage().run(job, ctx)

    def test_ingest_never_writes_to_the_mailbox(self, ctx, gmail, png_bytes) -> None:
        # The app holds gmail.readonly + gmail.send, not gmail.modify. If a future
        # change reintroduces a need for mailbox write, this fails loudly.
        from ytshort.integrations.gmail_client import GmailClient, GmailClientProtocol

        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = self._run(ctx)
        IngestStage().run(job, ctx)

        for mutator in ("ensure_label", "add_label", "modify", "trash", "delete"):
            assert not hasattr(gmail, mutator)
            assert not hasattr(GmailClient, mutator)
            assert mutator not in GmailClientProtocol.__annotations__

    def test_dedupe_survives_the_message_staying_in_the_inbox(
        self, ctx, gmail, png_bytes
    ) -> None:
        # Without labelling, the message keeps matching the Gmail query forever.
        # The job store is what stops it being processed twice.
        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = self._run(ctx)
        IngestStage().run(job, ctx)

        assert gmail.list_message_ids(ctx.settings.gmail_query, 10) == ["m1"]
        assert discover_jobs(ctx) == []

    def test_stage_is_idempotent_through_the_runner(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = self._run(ctx)
        runner = PipelineRunner([IngestStage()], ctx)

        runner.run(job)
        runner.run(job)

        assert len(job.attachments) == 1  # not downloaded twice
        assert job.state is JobState.ingested

    def test_job_id_matches_the_message(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = self._run(ctx)
        assert job.job_id == make_job_id("m1")


class TestPosterFrameFallback:
    """A video-only email used to halt at the thumbnail stage.

    The PRD accepts "Images and/or a video", so a video on its own has to work.
    The frame is derived here, during ingest, so that screening still sees it.
    """

    def _job(self, ctx):
        return discover_jobs(ctx)[0]

    def test_a_video_only_email_gets_a_derived_image(
        self, ctx, gmail, monkeypatch, tmp_path
    ) -> None:
        gmail.add_message("m1", subject="Clip", files={"clip.mp4": b"\x00\x00\x00\x20ftypmp42"})
        job = self._job(ctx)

        def fake_extract(self, video, out, *, at_seconds=None):
            out.write_bytes(b"\xff\xd8\xff\xe0 jpeg-ish")

        monkeypatch.setattr(FFmpeg, "extract_frame", fake_extract)
        IngestStage().run(job, ctx)

        images = [a for a in job.media_attachments if a.kind == "image"]
        assert len(images) == 1
        assert images[0].stored_path == "clip_poster.jpg"
        assert images[0].sha256
        assert job.media.primary_image == "clip_poster.jpg"
        assert job.media.primary_video == "clip.mp4"

    def test_the_derivation_is_recorded_as_a_finding(
        self, ctx, gmail, monkeypatch
    ) -> None:
        # The reviewer is approving a still they never sent; they should see that.
        gmail.add_message("m1", files={"clip.mp4": b"\x00\x00\x00\x20ftypmp42"})
        job = self._job(ctx)
        monkeypatch.setattr(
            FFmpeg, "extract_frame", lambda self, v, o, **kw: o.write_bytes(b"\xff\xd8\xff")
        )

        IngestStage().run(job, ctx)

        kinds = [f.kind for f in job.findings]
        assert "media.poster_frame_derived" in kinds

    def test_the_derived_frame_is_screened_like_any_other_image(
        self, ctx, gmail, monkeypatch
    ) -> None:
        """The reason this lives in ingest rather than in the thumbnail stage.

        Ingest runs before safety and pii, so the frame goes through moderation and
        OCR. Deriving it later would publish an unscreened still while
        ``pii.not_screened`` still claimed video frames were never examined.
        """
        gmail.add_message("m1", files={"clip.mp4": b"\x00\x00\x00\x20ftypmp42"})
        job = self._job(ctx)
        monkeypatch.setattr(
            FFmpeg, "extract_frame", lambda self, v, o, **kw: o.write_bytes(b"\xff\xd8\xff")
        )

        IngestStage().run(job, ctx)
        derived = next(a for a in job.media_attachments if a.kind == "image")

        # It is a normal accepted image attachment on the record, which is exactly
        # what SafetyStage and PiiStage iterate over.
        assert derived.is_media
        assert derived.accepted
        assert derived.stored_path is not None

    def test_an_email_with_an_image_derives_nothing(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message(
            "m1", files={"pic.png": png_bytes, "clip.mp4": b"\x00\x00\x00\x20ftypmp42"}
        )
        job = self._job(ctx)

        IngestStage().run(job, ctx)

        assert [f.kind for f in job.findings if "poster_frame" in f.kind] == []
        assert job.media.primary_image == "pic.png"

    def test_a_failed_extraction_falls_back_to_the_old_behaviour(
        self, ctx, gmail, monkeypatch
    ) -> None:
        # ffmpeg missing, or the video unreadable. Ingest must not start failing --
        # the thumbnail stage still halts, exactly as it did before.
        gmail.add_message("m1", files={"clip.mp4": b"\x00\x00\x00\x20ftypmp42"})
        job = self._job(ctx)

        def explode(self, video, out, *, at_seconds=None):
            raise FFmpegNotAvailable("ffmpeg is not installed")

        monkeypatch.setattr(FFmpeg, "extract_frame", explode)
        IngestStage().run(job, ctx)  # does not raise

        assert job.media.primary_image is None
        finding = next(f for f in job.findings if f.kind == "media.poster_frame_failed")
        assert finding.severity is Severity.warn
