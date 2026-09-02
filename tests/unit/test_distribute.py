"""Fan-out: an epilogue that must never fail the job, and never double-send."""

from __future__ import annotations

import json

from ytshort.config import Settings
from ytshort.contracts.models import Job, JobState, Publication, SourceEmail, make_job_id
from ytshort.sinks.registry import build_sinks
from ytshort.stages.distribute import DistributeStage


def _published(ctx, *, title: str = "Sunset") -> Job:
    job = Job(
        job_id=make_job_id("m1"),
        state=JobState.published,
        source=SourceEmail(message_id="m1", subject=title, sender="a@example.com"),
        title=title,
    )
    job.publication = Publication(
        video_id="vid1",
        watch_url="https://www.youtube.com/watch?v=vid1",
        short_url="https://youtu.be/vid1",
        privacy_status="private",
    )
    job.media.duration_seconds = 9.0
    job.media.audio_track = "track.mp3"
    ctx.job_store.save(job)
    return job


def _enable_email(ctx, monkeypatch, recipients: str = "me@example.com"):
    monkeypatch.setenv("YTSHORT_SINKS", "file,email")
    monkeypatch.setenv("YTSHORT_EMAIL_RECIPIENTS", recipients)
    ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
    ctx.settings.ensure_dirs()


class TestRegistry:
    def test_the_file_sink_is_always_present(self) -> None:
        assert [s.name for s in build_sinks(())] == ["file"]
        assert "file" in [s.name for s in build_sinks(("email",))]

    def test_unknown_sinks_are_ignored(self) -> None:
        assert [s.name for s in build_sinks(("file", "carrier-pigeon"))] == ["file"]

    def test_duplicates_are_collapsed(self) -> None:
        assert [s.name for s in build_sinks(("file", "file", "email"))] == ["file", "email"]


class TestFileSink:
    def test_writes_a_durable_record(self, ctx) -> None:
        job = _published(ctx)

        DistributeStage().run(job, ctx)

        record = json.loads(
            (ctx.settings.out_dir / f"{job.job_id}.json").read_text(encoding="utf-8")
        )
        assert record["short_url"] == "https://youtu.be/vid1"
        assert record["video_id"] == "vid1"
        assert job.state is JobState.published  # success_state is applied by the runner


class TestEmailSink:
    def test_sends_the_short_url(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx)

        DistributeStage().run(job, ctx)

        assert len(gmail.sent) == 1
        assert "https://youtu.be/vid1" in gmail.sent[0]["body_text"]
        assert gmail.sent[0]["to"] == ["me@example.com"]

    def test_html_escapes_a_hostile_subject(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx, title="<script>alert(1)</script>")

        DistributeStage().run(job, ctx)

        html = gmail.sent[0]["body_html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_private_upload_is_explained_to_the_recipient(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx)

        DistributeStage().run(job, ctx)

        assert "private" in gmail.sent[0]["body_text"]

    def test_missing_recipients_fail_only_that_sink(self, ctx, monkeypatch) -> None:
        monkeypatch.setenv("YTSHORT_SINKS", "file,email")
        monkeypatch.setenv("YTSHORT_EMAIL_RECIPIENTS", "")
        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env", strict=False)
        ctx.settings.ensure_dirs()
        job = _published(ctx)

        DistributeStage().run(job, ctx)

        results = {d.sink: d.ok for d in job.deliveries}
        assert results == {"file": True, "email": False}


class TestResilience:
    def test_a_throwing_sink_does_not_fail_the_job(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)

        def explode(*args, **kwargs):
            raise ConnectionError("smtp down")

        gmail.send_message = explode  # type: ignore[method-assign]
        job = _published(ctx)

        DistributeStage().run(job, ctx)  # must not raise

        email_result = next(d for d in job.deliveries if d.sink == "email")
        assert email_result.ok is False
        assert "smtp down" in email_result.detail
        # The other sink still delivered.
        assert next(d for d in job.deliveries if d.sink == "file").ok

    def test_a_successful_sink_is_not_re_delivered(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx)
        stage = DistributeStage()

        stage.run(job, ctx)
        stage.run(job, ctx)

        assert len(gmail.sent) == 1
        assert len(job.deliveries) == 2  # one row per sink, not per attempt

    def test_a_failed_sink_is_retried_on_the_next_run(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx)
        stage = DistributeStage()

        def explode(*args, **kwargs):
            raise ConnectionError("down")

        gmail.send_message = explode  # type: ignore[method-assign]
        stage.run(job, ctx)
        assert not next(d for d in job.deliveries if d.sink == "email").ok

        # Network comes back.
        gmail.send_message = lambda **kwargs: "sent-1"  # type: ignore[method-assign]
        stage.run(job, ctx)

        assert next(d for d in job.deliveries if d.sink == "email").ok
        assert len(job.deliveries) == 2

    def test_delivery_ids_are_stable_and_unique_per_sink(self, ctx) -> None:
        job = _published(ctx)
        assert job.delivery_id_for("file") == job.delivery_id_for("file")
        assert job.delivery_id_for("file") != job.delivery_id_for("email")

    def test_nothing_published_means_nothing_sent(self, ctx, gmail, monkeypatch) -> None:
        _enable_email(ctx, monkeypatch)
        job = _published(ctx)
        job.publication = None

        DistributeStage().run(job, ctx)

        assert gmail.sent == []
        assert job.deliveries == []
