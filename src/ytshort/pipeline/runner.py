"""Sequential stage runner with resume, persistence, and signal handling.

The runner is intentionally boring. It walks an ordered list of stages, skips the
ones already marked complete on the job record, times each one, persists after
every stage, and translates the three control signals into job states. All the
interesting behaviour lives in the stages themselves.

Resume is a direct consequence of "skip completed stages": re-running a job after
a crash, an approval, or a transient API failure picks up exactly where it left
off and never repeats work that already had an external side effect.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ccol import current

from ytshort.contracts.models import (
    Job,
    JobState,
    StageRecord,
    StageStatus,
    utcnow,
)
from ytshort.observability import instruments
from ytshort.observability.logging import get_logger, use_job
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure, SuspendPipeline
from ytshort.pipeline.stage import PipelineContext, Stage
from ytshort.storage.job_lock import job_lock

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger(__name__)


@dataclass
class RunOutcome:
    job: Job
    stages_run: list[str]
    stages_skipped: list[str]
    stopped_by: str | None = None
    reason: str = ""
    #: False when another runner already holds this job. Not an error -- the other
    #: process is dealing with it and this one moved on.
    lock_acquired: bool = True

    @property
    def parked(self) -> bool:
        return self.job.state is JobState.awaiting_review


class PipelineRunner:
    def __init__(self, stages: Sequence[Stage], ctx: PipelineContext) -> None:
        self.stages = list(stages)
        self.ctx = ctx

    def run(self, job: Job) -> RunOutcome:
        outcome = RunOutcome(job=job, stages_run=[], stages_skipped=[])

        # Two things can start a run in the deployed system: the scheduled Job and
        # an approval-triggered run. Without this lock both could execute publish
        # for the same job concurrently -- each reading the record before the other
        # writes it, so neither sees the other's video_id, and the video uploads
        # twice.
        with (
            use_job(job.job_id),
            current().span("pipeline.run", job_id=job.job_id),
            job_lock(self.ctx.settings.locks_dir, job.job_id) as acquired,
        ):
            if not acquired:
                outcome.lock_acquired = False
                outcome.reason = "another runner holds this job"
                log.info("job is locked by another runner, skipping")
                return outcome

            for stage in self.stages:
                if job.stage_completed(stage.name):
                    outcome.stages_skipped.append(stage.name)
                    log.debug("stage skipped (already complete)", extra={"stage": stage.name})
                    continue

                wait = self._backoff_remaining(job, stage.name)
                if wait is not None:
                    # Not a failure -- the job is simply not due yet. Leaving the
                    # state alone means the next tick picks it up unchanged.
                    outcome.stopped_by = stage.name
                    outcome.reason = f"backing off for {wait:.0f}s after a retryable failure"
                    log.info(
                        "stage is backing off",
                        extra={"stage": stage.name, "seconds_remaining": int(wait)},
                    )
                    break

                if not self._execute(stage, job, outcome):
                    break

            self.ctx.job_store.save(job)

        return outcome

    def _backoff_remaining(self, job: Job, stage_name: str) -> float | None:
        """Seconds still to wait before retrying ``stage_name``, or None if due."""
        previous = job.stages.get(stage_name)
        if previous is None or previous.retry_not_before is None:
            return None
        remaining = (previous.retry_not_before - utcnow()).total_seconds()
        return remaining if remaining > 0 else None

    def _retry_delay(self, attempts: int, floor_seconds: float) -> float:
        """Exponential backoff with jitter, capped.

        Jitter matters because the scheduled Job advances every pending job in one
        pass: without it, ten jobs failing against the same rate-limited API would
        retry in lockstep forever.
        """
        settings = self.ctx.settings
        delay = settings.retry_base_seconds * (2 ** max(attempts - 1, 0))
        delay = min(delay, settings.retry_max_seconds)
        delay = max(delay, floor_seconds)
        return delay * random.uniform(0.5, 1.0)

    def _execute(self, stage: Stage, job: Job, outcome: RunOutcome) -> bool:
        """Run one stage. Returns False when the pipeline must stop."""
        started = utcnow()
        clock = time.perf_counter()
        previous = job.stages.get(stage.name)
        attempt = (previous.attempts if previous else 0) + 1
        log.info("stage started", extra={"stage": stage.name, "attempt": attempt})

        def record(
            status: StageStatus,
            detail: str = "",
            retry_not_before: datetime | None = None,
        ) -> None:
            duration_ms = int((time.perf_counter() - clock) * 1000)
            job.stages[stage.name] = StageRecord(
                name=stage.name,
                status=status,
                started_at=started,
                completed_at=utcnow(),
                duration_ms=duration_ms,
                detail=detail,
                attempts=attempt,
                retry_not_before=retry_not_before,
            )
            # Recorded on every path, failures included -- a stage that times out
            # is exactly the one whose duration you want.
            instruments.stage_duration().record(
                duration_ms, {"stage": stage.name, "status": status.value}
            )

        def note_state() -> None:
            instruments.job_state_transitions().add(
                1, {"state": job.state.value, "stage": stage.name}
            )

        try:
            with current().span("stage", stage=stage.name, job_id=job.job_id):
                stage.run(job, self.ctx)
        except HaltPipeline as halt:
            record(StageStatus.halted, halt.reason)
            job.state = halt.state
            job.error = halt.reason
            note_state()
            outcome.stopped_by, outcome.reason = stage.name, halt.reason
            log.warning(
                "stage halted pipeline",
                extra={"stage": stage.name, "reason": halt.reason, "state": job.state},
            )
            self.ctx.job_store.save(job)
            return False
        except SuspendPipeline as suspend:
            record(StageStatus.suspended, suspend.reason)
            job.state = suspend.state
            note_state()
            outcome.stopped_by, outcome.reason = stage.name, suspend.reason
            log.info(
                "stage suspended pipeline",
                extra={"stage": stage.name, "reason": suspend.reason, "state": job.state},
            )
            self.ctx.job_store.save(job)
            return False
        except RetryableFailure as retry:
            # Deliberately NOT recorded as complete, so the next run retries it --
            # but only up to a point. A stage that fails every time is a broken
            # dependency, and asking it again on every tick forever costs money and
            # hammers someone else's API.
            if attempt >= self.ctx.settings.max_stage_attempts:
                record(StageStatus.failed, retry.reason)
                job.state = JobState.failed
                job.error = f"{stage.name}: gave up after {attempt} attempts: {retry.reason}"
                note_state()
                outcome.stopped_by, outcome.reason = stage.name, job.error
                log.error(
                    "stage exhausted its retries; dead-lettering the job",
                    extra={
                        "stage": stage.name,
                        "attempts": attempt,
                        "reason": retry.reason,
                    },
                )
                self.ctx.job_store.save(job)
                return False

            delay = self._retry_delay(attempt, retry.retry_after_seconds)
            record(
                StageStatus.failed,
                retry.reason,
                retry_not_before=utcnow() + timedelta(seconds=delay),
            )
            outcome.stopped_by, outcome.reason = stage.name, retry.reason
            log.warning(
                "stage failed, retryable",
                extra={
                    "stage": stage.name,
                    "reason": retry.reason,
                    "attempt": attempt,
                    "retry_in_seconds": int(delay),
                },
            )
            self.ctx.job_store.save(job)
            return False
        except Exception as exc:  # noqa: BLE001 - the runner is the last line of defence
            record(StageStatus.failed, repr(exc))
            job.state = JobState.failed
            job.error = f"{stage.name}: {exc!r}"
            note_state()
            outcome.stopped_by, outcome.reason = stage.name, repr(exc)
            log.exception("stage raised an unexpected error", extra={"stage": stage.name})
            self.ctx.job_store.save(job)
            return False

        record(StageStatus.completed)
        if stage.success_state is not None:
            job.state = stage.success_state
            note_state()
        outcome.stages_run.append(stage.name)
        self.ctx.job_store.save(job)
        log.info(
            "stage completed",
            extra={
                "stage": stage.name,
                "state": job.state,
                "ms": job.stages[stage.name].duration_ms,
            },
        )
        return True
