"""Always-on local sink: write the result record and print the link.

This exists so fan-out is testable and observable with no external account, and
so there is always at least one sink that cannot fail for network reasons. It is
also the audit trail -- one JSON file per published job, outside the job store.
"""

from __future__ import annotations

import json

from ytshort.contracts.models import Job, SinkResult
from ytshort.observability.logging import get_logger
from ytshort.pipeline.stage import PipelineContext
from ytshort.sinks.base import message_for

log = get_logger(__name__)


class FileSink:
    name = "file"

    def deliver(self, job: Job, ctx: PipelineContext) -> SinkResult:
        assert job.publication is not None
        subject, body = message_for(job)

        target = ctx.settings.out_dir / f"{job.job_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "job_id": job.job_id,
                    "title": job.title,
                    "short_url": job.publication.short_url,
                    "watch_url": job.publication.watch_url,
                    "video_id": job.publication.video_id,
                    "privacy_status": job.publication.privacy_status,
                    "source_subject": job.source.subject,
                    "source_sender": job.source.sender,
                    "published_at": job.publication.published_at.isoformat(),
                    "duration_seconds": job.media.duration_seconds,
                    "audio_track": job.media.audio_track,
                    "warnings": [f.model_dump(mode="json") for f in job.warn_findings],
                    "message": {"subject": subject, "body": body},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        log.info("short url", extra={"url": job.publication.short_url, "file": target.name})
        return SinkResult(
            sink=self.name,
            delivery_id=job.delivery_id_for(self.name),
            ok=True,
            detail=f"written to {target}",
        )
