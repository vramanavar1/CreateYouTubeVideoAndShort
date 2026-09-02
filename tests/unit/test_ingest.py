"""Discovery guardrails and attachment download."""

from __future__ import annotations

import pytest

from ytshort.contracts.models import JobState, Severity, make_job_id
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
