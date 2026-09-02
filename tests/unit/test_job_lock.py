"""The advisory lock that stops two runners publishing the same job twice."""

from __future__ import annotations

import json
import time

from ytshort.contracts.models import Job, JobState, SourceEmail, make_job_id
from ytshort.pipeline.runner import PipelineRunner
from ytshort.pipeline.stage import BaseStage
from ytshort.storage.job_lock import JobLock, job_lock


class CountingStage(BaseStage):
    name = "counting"
    success_state = JobState.published

    def __init__(self) -> None:
        self.calls = 0

    def run(self, job: Job, ctx) -> None:
        self.calls += 1


def _job() -> Job:
    return Job(job_id=make_job_id("m1"), source=SourceEmail(message_id="m1"))


class TestJobLock:
    def test_acquire_then_release(self, settings) -> None:
        lock = JobLock(settings.locks_dir, "job1")

        assert lock.acquire() is True
        assert lock.path.exists()

        lock.release()
        assert not lock.path.exists()

    def test_a_second_holder_is_refused(self, settings) -> None:
        first = JobLock(settings.locks_dir, "job1")
        second = JobLock(settings.locks_dir, "job1")

        assert first.acquire() is True
        assert second.acquire() is False  # refused, not raised

        first.release()
        assert second.acquire() is True

    def test_different_jobs_do_not_contend(self, settings) -> None:
        assert JobLock(settings.locks_dir, "job1").acquire() is True
        assert JobLock(settings.locks_dir, "job2").acquire() is True

    def test_lock_records_its_owner(self, settings) -> None:
        lock = JobLock(settings.locks_dir, "job1")
        lock.acquire()

        data = json.loads(lock.path.read_text(encoding="utf-8"))
        assert ":" in data["owner"]  # host:pid
        assert data["acquired_at"] <= time.time()

    def test_a_stale_lock_is_broken(self, settings) -> None:
        # A container killed mid-run must not wedge the job forever.
        abandoned = JobLock(settings.locks_dir, "job1")
        abandoned.acquire()
        abandoned.path.write_text(
            json.dumps({"owner": "dead-host:1", "acquired_at": time.time() - 10_000}),
            encoding="utf-8",
        )

        assert JobLock(settings.locks_dir, "job1", stale_after_seconds=60).acquire() is True

    def test_a_fresh_lock_is_not_broken(self, settings) -> None:
        held = JobLock(settings.locks_dir, "job1")
        held.acquire()

        assert JobLock(settings.locks_dir, "job1", stale_after_seconds=3600).acquire() is False

    def test_an_unreadable_lock_ages_out_by_mtime(self, settings) -> None:
        lock = JobLock(settings.locks_dir, "job1")
        lock.acquire()
        lock.path.write_text("{ corrupt", encoding="utf-8")

        # Not stale yet by mtime, so still respected.
        assert JobLock(settings.locks_dir, "job1", stale_after_seconds=3600).acquire() is False
        # Stale by mtime, so broken.
        assert JobLock(settings.locks_dir, "job1", stale_after_seconds=0).acquire() is True

    def test_release_is_safe_when_not_held(self, settings) -> None:
        JobLock(settings.locks_dir, "job1").release()  # must not raise

    def test_context_manager_reports_and_releases(self, settings) -> None:
        with job_lock(settings.locks_dir, "job1") as acquired:
            assert acquired is True
            with job_lock(settings.locks_dir, "job1") as second:
                assert second is False

        with job_lock(settings.locks_dir, "job1") as acquired:
            assert acquired is True

    def test_lock_is_released_even_when_the_body_raises(self, settings) -> None:
        try:
            with job_lock(settings.locks_dir, "job1"):
                raise ValueError("boom")
        except ValueError:
            pass

        assert JobLock(settings.locks_dir, "job1").acquire() is True


class TestRunnerIntegration:
    def test_a_locked_job_is_skipped_not_failed(self, ctx) -> None:
        job = _job()
        stage = CountingStage()

        holder = JobLock(ctx.settings.locks_dir, job.job_id)
        assert holder.acquire()

        outcome = PipelineRunner([stage], ctx).run(job)

        assert outcome.lock_acquired is False
        assert stage.calls == 0
        assert outcome.job.state is JobState.discovered  # untouched, not failed

    def test_the_lock_is_released_after_a_normal_run(self, ctx) -> None:
        job = _job()
        PipelineRunner([CountingStage()], ctx).run(job)

        assert JobLock(ctx.settings.locks_dir, job.job_id).acquire() is True

    def test_the_lock_is_released_after_a_stage_explodes(self, ctx) -> None:
        class Exploding(BaseStage):
            name = "boom"

            def run(self, job, ctx) -> None:
                raise ValueError("kaboom")

        job = _job()
        outcome = PipelineRunner([Exploding()], ctx).run(job)

        assert outcome.job.state is JobState.failed
        assert JobLock(ctx.settings.locks_dir, job.job_id).acquire() is True

    def test_concurrent_runs_execute_the_stage_once(self, ctx) -> None:
        # The scenario the lock exists for: two runners on one job. Without it
        # both would read "not yet published" and both would upload.
        job = _job()
        stage = CountingStage()
        runner = PipelineRunner([stage], ctx)

        holder = JobLock(ctx.settings.locks_dir, job.job_id)
        holder.acquire()
        first = runner.run(job)
        holder.release()
        second = runner.run(job)

        assert first.lock_acquired is False
        assert second.lock_acquired is True
        assert stage.calls == 1
