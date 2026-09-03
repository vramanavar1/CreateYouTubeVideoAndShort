"""Publishing: the one irreversible step, so mostly tests about not repeating it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ytshort.config import Settings
from ytshort.contracts.models import Job, JobState, Publication, Severity, SourceEmail, make_job_id
from ytshort.pipeline.runner import PipelineRunner
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure
from ytshort.stages.publish import PublishStage, build_description
from ytshort.stages.review import ReviewGateStage
from ytshort.stages.shorten import ShortenStage


def _publishable(ctx, *, title: str = "Sunset over the lake") -> Job:
    job = Job(
        job_id=make_job_id("m1"),
        state=JobState.approved,
        source=SourceEmail(message_id="m1", subject=title, body_snippet="A lovely evening"),
        title=title,
    )
    job.media.composed_video = "short.mp4"
    job.media.thumbnail_wide = "thumbnail_wide.jpg"
    job_dir = ctx.media_store.job_dir(job.job_id)
    (job_dir / "short.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (job_dir / "thumbnail_wide.jpg").write_bytes(b"\xff\xd8\xff")
    ctx.job_store.save(job)
    return job


class TestIdempotency:
    def test_a_job_that_already_has_a_video_id_is_not_re_uploaded(self, ctx, youtube) -> None:
        job = _publishable(ctx)
        job.publication = Publication(
            video_id="existing",
            watch_url="https://www.youtube.com/watch?v=existing",
            short_url="https://youtu.be/existing",
            privacy_status="private",
        )

        PublishStage().run(job, ctx)

        assert youtube.uploads == []
        assert job.publication.video_id == "existing"

    def test_running_the_stage_twice_uploads_once(self, ctx, youtube) -> None:
        job = _publishable(ctx)
        stage = PublishStage()

        stage.run(job, ctx)
        stage.run(job, ctx)

        assert len(youtube.uploads) == 1

    def test_the_video_id_survives_a_crash_after_upload(self, ctx, youtube) -> None:
        # The dangerous window: YouTube has the video but the job record does
        # not. The id must already be on disk, or a retry publishes a second copy.
        job = _publishable(ctx)

        def explode(video_id: str, path) -> bool:
            raise RuntimeError("process died setting the thumbnail")

        youtube.set_thumbnail = explode  # type: ignore[method-assign]

        outcome = PipelineRunner([PublishStage()], ctx).run(job)
        assert outcome.job.state is JobState.failed

        reloaded = ctx.job_store.load(job.job_id)
        assert reloaded is not None
        assert reloaded.publication is not None
        assert reloaded.publication.video_id == "vid0001"

        # Retrying the recovered job must not upload again.
        youtube.set_thumbnail = lambda video_id, path: True  # type: ignore[method-assign]
        PublishStage().run(reloaded, ctx)
        assert len(youtube.uploads) == 1


class TestUploadPayload:
    def test_sends_title_description_and_tags(self, ctx, youtube) -> None:
        job = _publishable(ctx)
        job.tags = ["travel"]

        PublishStage().run(job, ctx)

        upload = youtube.uploads[0]
        assert upload["title"] == "Sunset over the lake"
        assert "#Shorts" in upload["description"]
        assert "travel" in upload["tags"]
        assert upload["privacy_status"] == ctx.settings.privacy_status

    def test_title_is_capped_at_the_youtube_limit(self, ctx, youtube) -> None:
        job = _publishable(ctx, title="x" * 200)

        PublishStage().run(job, ctx)

        assert len(youtube.uploads[0]["title"]) == 100

    def test_description_always_carries_the_shorts_tag_exactly_once(self) -> None:
        job = Job(
            job_id="j",
            source=SourceEmail(message_id="m"),
            description="Already #shorts here",
        )
        assert build_description(job).lower().count("#shorts") == 1

    def test_urls_are_derived_from_the_video_id(self, ctx, youtube) -> None:
        job = _publishable(ctx)

        PublishStage().run(job, ctx)
        ShortenStage().run(job, ctx)

        assert job.publication is not None
        assert job.publication.short_url == f"https://youtu.be/{job.publication.video_id}"
        assert job.publication.video_id in job.publication.watch_url


class TestExternalConstraints:
    def test_a_forced_private_upload_is_recorded_as_a_warning(
        self, ctx, youtube, monkeypatch
    ) -> None:
        monkeypatch.setenv("YTSHORT_PRIVACY_STATUS", "public")
        ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
        ctx.settings.ensure_dirs()
        # An API project that has not passed YouTube's audit gets this.
        youtube.forced_privacy = "private"
        job = _publishable(ctx)

        PublishStage().run(job, ctx)

        assert job.publication is not None
        assert job.publication.privacy_status == "private"
        finding = next(f for f in job.findings if f.kind == "publish.privacy_overridden")
        assert finding.severity is Severity.warn
        assert "compliance audit" in finding.detail

    def test_a_rejected_thumbnail_does_not_fail_the_publish(self, ctx, youtube) -> None:
        youtube.thumbnail_succeeds = False
        job = _publishable(ctx)

        PublishStage().run(job, ctx)

        assert job.publication is not None
        assert job.publication.video_id
        assert job.publication.thumbnail_set is False
        assert any(f.kind == "publish.thumbnail_rejected" for f in job.findings)

    def test_an_upload_error_is_retryable(self, ctx, youtube) -> None:
        youtube.upload_error = ConnectionError("socket closed")
        job = _publishable(ctx)

        with pytest.raises(RetryableFailure, match="upload failed"):
            PublishStage().run(job, ctx)

        assert job.publication is None

    def test_a_missing_video_file_halts(self, ctx) -> None:
        job = _publishable(ctx)
        ctx.media_store.resolve(job.job_id, "short.mp4").unlink()

        with pytest.raises(HaltPipeline, match="missing from disk"):
            PublishStage().run(job, ctx)


class TestReviewGate:
    def test_an_unreviewed_job_suspends_before_publish(self, ctx, youtube) -> None:
        job = _publishable(ctx)
        job.state = JobState.composed
        job.review = None

        outcome = PipelineRunner([ReviewGateStage(), PublishStage()], ctx).run(job)

        assert outcome.job.state is JobState.awaiting_review
        assert youtube.uploads == []

    def test_a_rejected_job_never_publishes(self, ctx, youtube) -> None:
        from ytshort.runtime import record_decision

        job = _publishable(ctx)
        job.state = JobState.composed
        record_decision(job, ctx, decision="rejected", reason="blurry")

        outcome = PipelineRunner([ReviewGateStage(), PublishStage()], ctx).run(job)

        assert outcome.job.state is JobState.rejected
        assert youtube.uploads == []
        assert "blurry" in (outcome.job.error or "")

    def test_an_approved_job_proceeds(self, ctx, youtube) -> None:
        from ytshort.runtime import record_decision

        job = _publishable(ctx)
        job.state = JobState.composed
        record_decision(job, ctx, decision="approved")

        outcome = PipelineRunner([ReviewGateStage(), PublishStage()], ctx).run(job)

        assert outcome.job.state is JobState.published
        assert len(youtube.uploads) == 1


class TestAudioCredit:
    """Attribution required by a track's licence goes in every description.

    It is applied here rather than left to the reviewer because the obligation
    holds for every upload, and the one that gets forgotten is the one that draws
    the complaint. Most tracks need no credit, so empty is the normal case.
    """

    def _job(self, body: str = "A sunset over the lake.") -> Job:
        return Job(job_id="j", source=SourceEmail(message_id="m", body_snippet=body))

    def test_a_credit_is_appended_when_set(self) -> None:
        description = build_description(self._job(), "Track by Some Artist")

        assert "Track by Some Artist" in description

    def test_nothing_is_appended_when_empty(self) -> None:
        # The default, and the normal case for a "no attribution required" track.
        assert build_description(self._job(), "") == "A sunset over the lake.\n\n#Shorts"

    def test_whitespace_only_is_treated_as_empty(self) -> None:
        assert build_description(self._job(), "   ") == build_description(self._job())

    def test_a_credit_already_in_the_body_is_not_duplicated(self) -> None:
        # A reviewer who pasted the credit into the description themselves should
        # not end up with it twice.
        job = self._job("Filmed at dusk. Track by Some Artist")
        description = build_description(job, "Track by Some Artist")

        assert description.count("Track by Some Artist") == 1

    def test_the_credit_precedes_the_shorts_tag(self) -> None:
        # #Shorts has to stay last -- YouTube reads the description for it, and a
        # trailing credit would push it out of the visible preview.
        description = build_description(self._job(), "Track by Some Artist")

        assert description.index("Track by Some Artist") < description.index("#Shorts")

    def test_the_shorts_tag_is_still_added_exactly_once(self) -> None:
        description = build_description(self._job(), "Track by Some Artist")

        assert description.lower().count("#shorts") == 1

    def test_the_configured_credit_reaches_the_upload(self, ctx, youtube, monkeypatch) -> None:
        # End to end through the stage, not just the helper: this is what proves
        # settings.audio_credit is actually wired in.
        # Settings is a frozen dataclass, so replace the whole object rather than
        # patching an attribute -- the instance value would shadow a class patch.
        monkeypatch.setattr(
            ctx, "settings", replace(ctx.settings, audio_credit="Track by Some Artist")
        )
        PublishStage().run(_publishable(ctx), ctx)

        assert "Track by Some Artist" in youtube.uploads[0]["description"]
