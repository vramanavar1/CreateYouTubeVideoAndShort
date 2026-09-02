"""The human gate. Nothing reaches YouTube without passing through here.

The stage itself is four lines of logic, which is the point: the decision lives
with a person, recorded on the job as a ``Review``. Everything before this stage
is preparation; everything after it is irreversible.

Note that a suspended stage is *not* recorded as complete, so re-running the job
after approval re-enters this stage, sees the recorded decision, and moves on.
"""

from __future__ import annotations

from ytshort.contracts.models import Job, JobState
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline, SuspendPipeline
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)


class ReviewGateStage(BaseStage):
    name = "review"
    success_state = JobState.approved

    def run(self, job: Job, ctx: PipelineContext) -> None:
        if job.review is None:
            raise SuspendPipeline("waiting for human approval")

        if job.review.decision == "rejected":
            raise HaltPipeline(
                f"rejected by {job.review.reviewer}: {job.review.reason or 'no reason given'}",
                state=JobState.rejected,
            )

        log.info(
            "approved for publication",
            extra={"reviewer": job.review.reviewer, "title": job.title[:60]},
        )
