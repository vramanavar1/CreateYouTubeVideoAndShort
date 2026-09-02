"""Fan the short URL out to every configured sink.

This stage is an *epilogue*: it never fails the job. The video is already public
(or private-locked) on YouTube by the time it runs, and a job driven back into
``failed`` because a notification bounced would be both wrong and dangerous --
a retry of a failed job re-enters the pipeline.

Delivery is per-sink idempotent: a sink that already succeeded is skipped, so
re-running after one sink failed retries only that one.
"""

from __future__ import annotations

from ytshort.contracts.models import Job, JobState, SinkResult
from ytshort.observability.logging import get_logger
from ytshort.pipeline.stage import BaseStage, PipelineContext
from ytshort.sinks.registry import build_sinks

log = get_logger(__name__)


class DistributeStage(BaseStage):
    name = "distribute"
    success_state = JobState.done

    def run(self, job: Job, ctx: PipelineContext) -> None:
        if job.publication is None:
            log.warning("distribute ran with nothing published; skipping")
            return

        for sink in build_sinks(ctx.settings.sinks):
            if job.delivered(sink.name):
                log.debug("sink already delivered, skipping", extra={"sink": sink.name})
                continue

            try:
                result = sink.deliver(job, ctx)
            except Exception as exc:  # noqa: BLE001 - a sink must never fail the job
                log.warning(
                    "sink delivery failed",
                    extra={"sink": sink.name, "error": repr(exc)},
                )
                result = SinkResult(
                    sink=sink.name,
                    delivery_id=job.delivery_id_for(sink.name),
                    ok=False,
                    detail=repr(exc),
                )

            # Replace any earlier failed attempt for this sink rather than piling
            # up one row per retry.
            job.deliveries = [d for d in job.deliveries if d.sink != sink.name]
            job.deliveries.append(result)

        delivered = sum(1 for d in job.deliveries if d.ok)
        log.info(
            "distribution complete",
            extra={"delivered": delivered, "attempted": len(job.deliveries)},
        )
