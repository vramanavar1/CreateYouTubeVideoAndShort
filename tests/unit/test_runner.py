"""The runner's contract: resume, signal handling, and persistence."""

from __future__ import annotations

from datetime import timedelta

from tests.fakes import RecordingInstrument
from ytshort.contracts.models import (
    Job,
    JobState,
    SourceEmail,
    StageStatus,
    make_job_id,
    utcnow,
)
from ytshort.observability import instruments
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

        # The retry is now deferred rather than immediate, so wind the clock past
        # the backoff window before expecting the second attempt.
        job.stages["flaky"].retry_not_before = utcnow() - timedelta(seconds=1)
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


class TestRunnerMetrics:
    """Stage timing and state transitions reach telemetry, on every path."""

    def test_stage_duration_is_recorded_with_stage_and_status(
        self, ctx, monkeypatch
    ) -> None:
        durations = RecordingInstrument()
        monkeypatch.setattr(instruments, "stage_duration", lambda: durations)

        PipelineRunner([RecordingStage("one", JobState.ingested)], ctx).run(_job())

        assert len(durations.points) == 1
        value, attributes = durations.points[0]
        assert value >= 0
        assert attributes == {"stage": "one", "status": "completed"}

    def test_a_failing_stage_still_records_its_duration(self, ctx, monkeypatch) -> None:
        # The failure path is the one you most want timings for -- a stage that
        # hangs and then blows up is invisible if only successes are measured.
        durations = RecordingInstrument()
        monkeypatch.setattr(instruments, "stage_duration", lambda: durations)

        stages = [RecordingStage("one", raises=RetryableFailure("transient"))]
        PipelineRunner(stages, ctx).run(_job())

        assert durations.attributes_for("status") == ["failed"]

    def test_a_halted_stage_records_duration_and_the_new_state(
        self, ctx, monkeypatch
    ) -> None:
        durations = RecordingInstrument()
        transitions = RecordingInstrument()
        monkeypatch.setattr(instruments, "stage_duration", lambda: durations)
        monkeypatch.setattr(instruments, "job_state_transitions", lambda: transitions)

        stages = [RecordingStage("one", raises=HaltPipeline("bad", JobState.quarantined))]
        PipelineRunner(stages, ctx).run(_job())

        assert durations.attributes_for("status") == ["halted"]
        assert transitions.attributes_for("state") == ["quarantined"]

    def test_state_transitions_are_counted_once_per_change(self, ctx, monkeypatch) -> None:
        transitions = RecordingInstrument()
        monkeypatch.setattr(instruments, "job_state_transitions", lambda: transitions)

        stages = [
            RecordingStage("one", JobState.ingested),
            RecordingStage("two"),  # annotating only -- no state change, no count
            RecordingStage("three", JobState.screened),
        ]
        PipelineRunner(stages, ctx).run(_job())

        assert transitions.attributes_for("state") == ["ingested", "screened"]

    def test_metrics_are_safe_when_telemetry_is_unconfigured(self, ctx) -> None:
        # The default path in every other test in this repo: no exporter, so the
        # instruments are no-ops and the run must be entirely unaffected.
        outcome = PipelineRunner([RecordingStage("one", JobState.ingested)], ctx).run(_job())

        assert outcome.stages_run == ["one"]


class TestBoundedRetries:
    """Retries are finite and spaced out.

    Before this, a stage whose dependency was permanently broken re-ran on every
    scheduled tick forever -- a self-inflicted DoS against whatever it was calling,
    and a cost that grows on its own.
    """

    def _due(self, job: Job, stage: str) -> Job:
        """Wind the clock past the backoff window."""
        job.stages[stage].retry_not_before = utcnow() - timedelta(seconds=1)
        return job

    def test_a_retry_is_deferred_rather_than_immediate(self, ctx) -> None:
        flaky = RecordingStage("flaky", raises=RetryableFailure("timeout"))
        runner = PipelineRunner([flaky], ctx)

        job = runner.run(_job()).job
        assert job.stages["flaky"].retry_not_before is not None

        outcome = runner.run(job)  # same tick; still inside the backoff window
        assert flaky.calls == 1
        assert "backing off" in outcome.reason

    def test_attempts_accumulate_across_runs(self, ctx) -> None:
        flaky = RecordingStage("flaky", raises=RetryableFailure("timeout"))
        runner = PipelineRunner([flaky], ctx)

        job = runner.run(_job()).job
        assert job.stages["flaky"].attempts == 1
        job = runner.run(self._due(job, "flaky")).job
        assert job.stages["flaky"].attempts == 2

    def test_it_dead_letters_once_attempts_are_exhausted(self, ctx) -> None:
        flaky = RecordingStage("flaky", raises=RetryableFailure("always down"))
        runner = PipelineRunner([flaky], ctx)
        cap = ctx.settings.max_stage_attempts

        job = _job()
        for _ in range(cap):
            job = runner.run(job).job
            if job.state is JobState.failed:
                break
            self._due(job, "flaky")

        assert flaky.calls == cap
        assert job.state is JobState.failed
        assert "gave up after" in (job.error or "")

    def test_a_dead_lettered_job_is_terminal(self, ctx) -> None:
        # Terminal matters: `ytshort run` only advances non-terminal jobs, so this
        # is what actually stops the loop.
        flaky = RecordingStage("flaky", raises=RetryableFailure("always down"))
        runner = PipelineRunner([flaky], ctx)

        job = _job()
        for _ in range(ctx.settings.max_stage_attempts):
            job = runner.run(job).job
            self._due(job, "flaky")

        assert job.is_terminal

    def test_a_successful_retry_clears_the_backoff(self, ctx) -> None:
        flaky = RecordingStage("flaky", JobState.ingested, raises=RetryableFailure("blip"))
        runner = PipelineRunner([flaky], ctx)

        job = runner.run(_job()).job
        flaky._raises = None
        job = runner.run(self._due(job, "flaky")).job

        assert job.stages["flaky"].status is StageStatus.completed
        assert job.stages["flaky"].retry_not_before is None

    def test_the_delay_is_bounded_and_jittered(self, ctx) -> None:
        runner = PipelineRunner([], ctx)
        cap = ctx.settings.retry_max_seconds

        # Jitter, so ten jobs failing against one rate-limited API do not retry in
        # lockstep. Never above the cap, never zero.
        delays = {runner._retry_delay(20, 0.0) for _ in range(50)}
        assert len(delays) > 1
        assert all(0 < delay <= cap for delay in delays)

    def test_a_service_supplied_floor_is_respected(self, ctx) -> None:
        runner = PipelineRunner([], ctx)
        # retry_after_seconds used to be dead code. It is now the floor for the
        # backoff -- what a Retry-After header would feed.
        assert runner._retry_delay(1, 7200.0) >= 3600.0
