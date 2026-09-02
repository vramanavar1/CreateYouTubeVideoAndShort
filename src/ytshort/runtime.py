"""Wiring shared by the CLI and the review UI.

Both entry points need the same thing: a ``PipelineContext`` holding the stores
and, where credentials allow, live Google clients. Building the clients is
tolerant by design -- a missing or expired token leaves them ``None`` and logs
why, so ``ytshort status`` and the review UI still work on a box that has not
been authorised yet. Stages that genuinely need a client fail with a clear
message when they run.
"""

from __future__ import annotations

from ytshort.config import Settings
from ytshort.contracts.models import Job, JobState, Review
from ytshort.integrations.google_auth import AuthError
from ytshort.integrations.moderation import build_moderator
from ytshort.integrations.scanner import build_scanner
from ytshort.observability.logging import get_logger
from ytshort.observability.setup import configure_observability
from ytshort.pipeline.runner import PipelineRunner, RunOutcome
from ytshort.pipeline.stage import PipelineContext
from ytshort.stages import build_stages
from ytshort.storage.job_store import JobStore
from ytshort.storage.media_store import MediaStore

log = get_logger(__name__)


def bootstrap(settings: Settings | None = None) -> Settings:
    """Load settings, create runtime directories, and configure observability."""
    settings = settings or Settings.load()
    settings.ensure_dirs()
    configure_observability(settings)
    return settings


def build_context(
    settings: Settings,
    *,
    with_google: bool = True,
    allow_interactive: bool = False,
) -> PipelineContext:
    gmail = None
    youtube = None

    if with_google:
        try:
            from ytshort.integrations.gmail_client import GmailClient
            from ytshort.integrations.youtube_client import YouTubeClient

            gmail = GmailClient.build(settings, allow_interactive=allow_interactive)
            youtube = YouTubeClient.build(settings, allow_interactive=allow_interactive)
        except AuthError as exc:
            log.warning("google clients unavailable", extra={"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never let wiring kill the process
            log.warning("could not build google clients", extra={"error": repr(exc)})

    return PipelineContext(
        settings=settings,
        job_store=JobStore(settings.jobs_dir),
        media_store=MediaStore(settings.media_dir),
        gmail=gmail,
        youtube=youtube,
        scanner=build_scanner(settings.malware_scanner, settings.virustotal_api_key),
        moderator=build_moderator(settings.moderation_provider, settings.anthropic_api_key),
    )


def run_job(job: Job, ctx: PipelineContext) -> RunOutcome:
    return PipelineRunner(build_stages(), ctx).run(job)


def resume_job(job_id: str, ctx: PipelineContext) -> RunOutcome | None:
    job = ctx.job_store.load(job_id)
    if job is None:
        return None
    return run_job(job, ctx)


def record_decision(
    job: Job,
    ctx: PipelineContext,
    *,
    decision: str,
    reviewer: str = "local",
    reason: str = "",
) -> Job:
    """Attach a review decision and move the job out of ``awaiting_review``.

    Kept here rather than in the web layer so the CLI and the UI record decisions
    identically -- there is exactly one way a job becomes approved.
    """
    job.review = Review(
        decision="approved" if decision == "approved" else "rejected",
        reviewer=reviewer,
        reason=reason,
    )
    # Move the job out of awaiting_review immediately, before any publishing
    # happens. Publishing may be minutes away -- delegated to the scheduled Job --
    # and leaving the state as awaiting_review in the meantime would let the same
    # job be approved a second time, triggering a second publish attempt.
    job.state = (
        JobState.rejected if job.review.decision == "rejected" else JobState.approved
    )
    ctx.job_store.save(job)
    return job
