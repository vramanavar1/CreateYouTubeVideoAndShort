"""Upload the composed Short to YouTube.

The only stage with an irreversible external side effect, so it is also the most
defensive one:

* If ``job.publication`` already carries a ``video_id``, the upload is skipped
  entirely. A retry after a crash between "YouTube accepted the video" and "the
  job record was saved" can never publish a second copy.
* A failed custom thumbnail does not fail the publish -- the video exists, and
  pretending otherwise would trigger a retry that uploads it again.
"""

from __future__ import annotations

from ytshort.contracts.models import Finding, Job, JobState, Publication, Severity
from ytshort.integrations.faults import is_transient
from ytshort.observability import instruments
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)

SHORTS_TAG = "#Shorts"


def build_description(job: Job) -> str:
    """Description seeded from the email body, always carrying the Shorts tag."""
    parts = [job.description.strip() or job.source.body_snippet.strip()]
    body = "\n\n".join(part for part in parts if part)
    if SHORTS_TAG.lower() not in body.lower():
        body = f"{body}\n\n{SHORTS_TAG}".strip()
    return body


class PublishStage(BaseStage):
    name = "publish"
    success_state = JobState.published

    def run(self, job: Job, ctx: PipelineContext) -> None:
        if job.publication and job.publication.video_id:
            log.info(
                "already published, skipping upload",
                extra={"video_id": job.publication.video_id},
            )
            return

        if ctx.youtube is None:
            raise RuntimeError("publish requires a YouTube client")
        if not job.media.composed_video:
            raise HaltPipeline("nothing to publish: no composed video on the job")

        settings = ctx.settings
        video_path = ctx.media_store.resolve(job.job_id, job.media.composed_video)
        if not video_path.exists():
            raise HaltPipeline(f"composed video is missing from disk: {video_path.name}")

        title = (job.title or job.source.subject or "Untitled Short").strip()[:100]
        description = build_description(job)
        tags = list(dict.fromkeys([*job.tags, *settings.video_tags]))

        try:
            result = ctx.youtube.upload_video(
                video_path,
                title=title,
                description=description,
                tags=tags,
                category_id=settings.video_category_id,
                privacy_status=settings.privacy_status,
            )
        except Exception as exc:  # noqa: BLE001 - classified below, never blindly retried
            if not is_transient(exc):
                # A 403 for an exhausted quota, an unverified channel or a revoked
                # credential returns the same answer on every scheduled run. Retrying
                # it forever is a self-inflicted DoS against YouTube; halt instead so
                # a human sees it.
                raise HaltPipeline(
                    f"upload rejected and will not succeed on retry: {exc!r}",
                    JobState.failed,
                ) from exc
            raise RetryableFailure(f"upload failed: {exc!r}") from exc

        job.publication = Publication(
            video_id=result.video_id,
            watch_url=f"https://www.youtube.com/watch?v={result.video_id}",
            short_url=f"https://youtu.be/{result.video_id}",
            privacy_status=result.uploaded_privacy_status,
        )
        job.title = title
        job.description = description
        # Persist immediately: everything below this line is optional, and the
        # video_id must survive even if the process dies in the next statement.
        ctx.job_store.save(job)

        instruments.publish_outcomes().add(
            1,
            {
                "requested": settings.privacy_status,
                "applied": result.uploaded_privacy_status,
            },
        )

        if result.uploaded_privacy_status != settings.privacy_status:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="publish.privacy_overridden",
                    severity=Severity.warn,
                    where=result.video_id,
                    detail=(
                        f"requested '{settings.privacy_status}' but YouTube set "
                        f"'{result.uploaded_privacy_status}'. Uploads from an API project "
                        "that has not passed YouTube's compliance audit are locked to "
                        "private."
                    ),
                    action_taken="published anyway",
                )
            )

        if job.media.thumbnail_wide:
            thumbnail = ctx.media_store.resolve(job.job_id, job.media.thumbnail_wide)
            job.publication.thumbnail_set = ctx.youtube.set_thumbnail(
                result.video_id, thumbnail
            )
            if not job.publication.thumbnail_set:
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="publish.thumbnail_rejected",
                        severity=Severity.warn,
                        where=result.video_id,
                        detail=(
                            "the custom thumbnail was rejected -- this usually means the "
                            "channel is not phone-verified"
                        ),
                        action_taken="video published with an auto-generated thumbnail",
                    )
                )

        log.info(
            "published",
            extra={
                "video_id": result.video_id,
                "privacy": result.uploaded_privacy_status,
                "thumbnail_set": job.publication.thumbnail_set,
            },
        )
