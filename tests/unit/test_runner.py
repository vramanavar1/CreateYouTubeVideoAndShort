"""The runner's contract: resume, signal handling, and persistence."""

from __future__ import annotations

from ytshort.contracts.models import Job, JobState, SourceEmail, StageStatus, make_job_id
from ytshort.pipeline.runner import PipelineRunner
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure, SuspendPipeline
from ytshort.pipeline.stage import BaseStage


class RecordingStage(BaseStage):
    def __init__(self, name: str, state: JobState | None = None, raises: Exception | None = None):
        self.name = name
        self.success_state = state
        self._raises = raises
        self.calls = 0

    def run(self, job: Job, ctx) -> None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises


def _job() -> Job:
    return Job(job_id=make_job_id("m1"), source=SourceEmail(message_id="m1"))


class TestRunner:
    def test_runs_stages_in_order_and_applies_success_state(self, ctx) -> None:
        stages = [
            RecordingStage("one", JobState.ingested),
            RecordingStage("two", JobState.screened),
        ]
        outcome = PipelineRunner(stages, ctx).run(_job())

        assert outcome.stages_run == ["one", "two"]
        assert outcome.job.state is JobState.screened

    def test_completed_stages_are_skipped_on_a_second_run(self, ctx) -> None:
        stages = [RecordingStage("one", JobState.ingested), RecordingStage("two")]
        runner = PipelineRunner(stages, ctx)

        job = runner.run(_job()).job
        outcome = runner.run(job)

        assert outcome.stages_run == []
        assert outcome.stages_skipped == ["one", "two"]
        assert stages[0].calls == 1  # not re-executed

    def test_halt_lands_in_the_signalled_terminal_state(self, ctx) -> None:
        stages = [
            RecordingStage("one", JobState.ingested),
            RecordingStage("two", raises=HaltPipeline("nope")),
            RecordingStage("three"),
        ]
        outcome = PipelineRunner(stages, ctx).run(_job())

        assert outcome.job.state is JobState.quarantined
        assert outcome.stopped_by == "two"
        assert outcome.job.error == "nope"
        assert stages[2].calls == 0  # everything after a halt is skipped

    def test_halt_can_choose_its_state(self, ctx) -> None:
        stages = [RecordingStage("one", raises=HaltPipeline("no", state=JobState.rejected))]
        assert PipelineRunner(stages, ctx).run(_job()).job.state is JobState.rejected

    def test_suspend_parks_the_job_and_is_re_entered_on_resume(self, ctx) -> None:
        gate = RecordingStage("gate", raises=SuspendPipeline("waiting"))
        after = RecordingStage("after", JobState.published)
        runner = PipelineRunner([gate, after], ctx)

        job = runner.run(_job()).job
        assert job.state is JobState.awaiting_review
        assert job.stages["gate"].status is StageStatus.suspended
        assert after.calls == 0

        # The human decides; the gate now passes and the pipeline continues.
        gate._raises = None
        outcome = runner.run(job)

        assert gate.calls == 2  # a suspended stage is NOT marked complete
        assert outcome.job.state is JobState.published

    def test_retryable_failure_leaves_the_stage_incomplete(self, ctx) -> None:
        flaky = RecordingStage("flaky", JobState.ingested, raises=RetryableFailure("timeout"))
        runner = PipelineRunner([flaky], ctx)

        job = runner.run(_job()).job
        assert job.stages["flaky"].status is StageStatus.failed
        assert job.state is JobState.discovered  # not moved on, not marked failed

        flaky._raises = None
        assert runner.run(job).job.state is JobState.ingested
        assert flaky.calls == 2

    def test_unexpected_exception_fails_the_job_loudly(self, ctx) -> None:
        stages = [RecordingStage("boom", raises=ValueError("kaboom"))]
        outcome = PipelineRunner(stages, ctx).run(_job())

        assert outcome.job.state is JobState.failed
        assert "kaboom" in (outcome.job.error or "")

    def test_job_is_persisted_after_every_stage(self, ctx) -> None:
        stages = [RecordingStage("one", JobState.ingested), RecordingStage("two")]
        job = PipelineRunner(stages, ctx).run(_job()).job

        reloaded = ctx.job_store.load(job.job_id)
        assert reloaded is not None
        assert set(reloaded.stages) == {"one", "two"}
