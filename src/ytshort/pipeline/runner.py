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

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ytshort.contracts.models import (
    Job,
    JobState,
    StageRecord,
    StageStatus,
    utcnow,
)
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
        with use_job(job.job_id), job_lock(self.ctx.settings.locks_dir, job.job_id) as acquired:
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

                if not self._execute(stage, job, outcome):
                    break

            self.ctx.job_store.save(job)

        return outcome

    def _execute(self, stage: Stage, job: Job, outcome: RunOutcome) -> bool:
        """Run one stage. Returns False when the pipeline must stop."""
        started = utcnow()
        clock = time.perf_counter()
        log.info("stage started", extra={"stage": stage.name})

        def record(status: StageStatus, detail: str = "") -> None:
            job.stages[stage.name] = StageRecord(
                name=stage.name,
                status=status,
                started_at=started,
                completed_at=utcnow(),
                duration_ms=int((time.perf_counter() - clock) * 1000),
                detail=detail,
            )

        try:
            stage.run(job, self.ctx)
        except HaltPipeline as halt:
            record(StageStatus.halted, halt.reason)
            job.state = halt.state
            job.error = halt.reason
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
            outcome.stopped_by, outcome.reason = stage.name, suspend.reason
            log.info(
                "stage suspended pipeline",
                extra={"stage": stage.name, "reason": suspend.reason, "state": job.state},
            )
            self.ctx.job_store.save(job)
            return False
        except RetryableFailure as retry:
            # Deliberately NOT recorded as complete, so the next run retries it.
            record(StageStatus.failed, retry.reason)
            outcome.stopped_by, outcome.reason = stage.name, retry.reason
            log.warning(
                "stage failed, retryable",
                extra={"stage": stage.name, "reason": retry.reason},
            )
            self.ctx.job_store.save(job)
            return False
        except Exception as exc:  # noqa: BLE001 - the runner is the last line of defence
            record(StageStatus.failed, repr(exc))
            job.state = JobState.failed
            job.error = f"{stage.name}: {exc!r}"
            outcome.stopped_by, outcome.reason = stage.name, repr(exc)
            log.exception("stage raised an unexpected error", extra={"stage": stage.name})
            self.ctx.job_store.save(job)
            return False

        record(StageStatus.completed)
        if stage.success_state is not None:
            job.state = stage.success_state
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
