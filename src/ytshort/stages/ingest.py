"""Discover candidate emails and pull their attachments.

Two pieces live here:

``discover_jobs`` is not a stage -- it *creates* jobs, so it runs before the
runner. It enforces the PRD's "max 10 emails per day" cap and the dedupe rule
that one Gmail message can only ever produce one job.

``IngestStage`` then downloads that job's attachments, enforcing the 20 MB total
and the media-type allow-list.
"""

from __future__ import annotations

from pathlib import Path

from ytshort.contracts.models import (
    Attachment,
    Finding,
    Job,
    JobState,
    Severity,
    SourceEmail,
    make_job_id,
)
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline, RetryableFailure
from ytshort.pipeline.stage import BaseStage, PipelineContext
from ytshort.storage.counters import DailyCounter

log = get_logger(__name__)


def _sender_address(raw: str) -> str:
    """Extract the bare address from a ``Name <addr@example.com>`` header."""
    if "<" in raw and ">" in raw:
        return raw[raw.rindex("<") + 1 : raw.rindex(">")].strip().lower()
    return raw.strip().lower()


def discover_jobs(ctx: PipelineContext, limit: int | None = None) -> list[Job]:
    """List new candidate mail and create a Job record for each.

    Respects the daily cap, the sender allow-list, and the "one job per message"
    rule. Returns only newly created jobs.
    """
    if ctx.gmail is None:
        raise RuntimeError("discover_jobs requires a Gmail client")

    settings = ctx.settings
    counter = DailyCounter(settings.counters_dir)
    remaining = counter.remaining(settings.max_emails_per_day)
    if limit is not None:
        remaining = min(remaining, limit)

    if remaining <= 0:
        log.info(
            "daily email cap reached, nothing will be ingested",
            extra={"cap": settings.max_emails_per_day, "used": counter.count()},
        )
        return []

    # Over-fetch: some listed messages will already have jobs or fail the sender
    # allow-list, and those must not consume the day's budget.
    candidate_ids = ctx.gmail.list_message_ids(settings.gmail_query, max_results=remaining * 4)
    known = ctx.job_store.known_message_ids()

    created: list[Job] = []
    for message_id in candidate_ids:
        if len(created) >= remaining:
            break
        if message_id in known:
            continue

        message = ctx.gmail.get_message(message_id)

        if settings.allowed_senders:
            sender = _sender_address(message.sender)
            if sender not in settings.allowed_senders:
                log.info(
                    "skipping message from non-allowed sender",
                    extra={"sender": sender, "message_id": message_id},
                )
                continue

        if not message.attachments:
            log.debug("skipping message with no attachments", extra={"message_id": message_id})
            continue

        job = Job(
            job_id=make_job_id(message_id),
            state=JobState.discovered,
            source=SourceEmail(
                message_id=message_id,
                thread_id=message.thread_id,
                sender=message.sender,
                subject=message.subject,
                body_snippet=message.snippet,
                received_at=message.received_at,
            ),
            title=message.subject.strip(),
        )
        ctx.job_store.save(job)
        counter.increment()
        created.append(job)
        log.info(
            "job created from email",
            extra={"job": job.job_id, "subject": job.source.subject[:60]},
        )

    return created


class IngestStage(BaseStage):
    name = "ingest"
    success_state = JobState.ingested

    def run(self, job: Job, ctx: PipelineContext) -> None:
        if ctx.gmail is None:
            raise RuntimeError("ingest requires a Gmail client")

        settings = ctx.settings
        message = ctx.gmail.get_message(job.source.message_id)

        # Check the *declared* sizes first. Downloading 100 MB only to reject it
        # wastes bandwidth and quota, and the PRD cap is about what arrives.
        declared_total = sum(a.size_bytes for a in message.attachments)
        if declared_total > settings.max_total_attachment_bytes:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="attachments.too_large",
                    severity=Severity.blocking,
                    where="email",
                    detail=(
                        f"attachments total {declared_total} bytes, "
                        f"limit is {settings.max_total_attachment_bytes}"
                    ),
                    action_taken="rejected before download",
                )
            )
            raise HaltPipeline(
                f"attachments exceed the {settings.max_total_attachment_bytes}-byte limit"
            )

        downloaded_total = 0
        for meta in message.attachments:
            suffix = Path(meta.filename).suffix.lower()
            kind = _classify(suffix, settings)

            attachment = Attachment(
                attachment_id=meta.attachment_id,
                filename=meta.filename,
                declared_mime=meta.mime_type,
                size_bytes=meta.size_bytes,
                kind=kind,
            )

            if kind == "other":
                attachment.accepted = False
                attachment.reject_reason = f"file type {suffix or '(none)'} is not allowed"
                job.attachments.append(attachment)
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="attachment.type_not_allowed",
                        severity=Severity.info,
                        where=meta.filename,
                        detail=attachment.reject_reason,
                        action_taken="skipped",
                    )
                )
                continue

            try:
                data = ctx.gmail.get_attachment(job.source.message_id, meta.attachment_id)
            except Exception as exc:  # noqa: BLE001 - network faults are retryable
                raise RetryableFailure(
                    f"could not download attachment {meta.filename!r}: {exc!r}"
                ) from exc

            downloaded_total += len(data)
            if downloaded_total > settings.max_total_attachment_bytes:
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="attachments.too_large",
                        severity=Severity.blocking,
                        where=meta.filename,
                        detail=(
                            f"downloaded bytes reached {downloaded_total}, "
                            f"limit is {settings.max_total_attachment_bytes}"
                        ),
                        action_taken="download aborted",
                    )
                )
                raise HaltPipeline("attachments exceed the size limit once downloaded")

            path, digest = ctx.media_store.write_bytes(job.job_id, meta.filename, data)
            attachment.stored_path = path.name
            attachment.sha256 = digest
            attachment.size_bytes = len(data)
            job.attachments.append(attachment)
            log.info(
                "attachment stored",
                extra={"file": path.name, "bytes": len(data), "kind": kind},
            )

        media = job.media_attachments
        if not media:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="attachments.no_usable_media",
                    severity=Severity.blocking,
                    where="email",
                    detail="no image or video attachment survived the allow-list",
                    action_taken="halted",
                )
            )
            raise HaltPipeline("email contained no usable image or video")

        # First of each kind wins. The thumbnail needs an image; the body prefers
        # a video and falls back to a still.
        for attachment in media:
            if attachment.kind == "image" and job.media.primary_image is None:
                job.media.primary_image = attachment.stored_path
            elif attachment.kind == "video" and job.media.primary_video is None:
                job.media.primary_video = attachment.stored_path

        if not job.title:
            job.title = job.source.subject.strip() or "Untitled Short"

        # Note: the message is deliberately NOT labelled or marked read. That would
        # need gmail.modify -- write access to the whole mailbox -- to achieve
        # something JobStore.known_message_ids() already does. The Gmail query is
        # date-bounded instead, so the listing stays small.


def _classify(suffix: str, settings) -> str:
    if suffix in settings.image_extensions:
        return "image"
    if suffix in settings.video_extensions:
        return "video"
    return "other"
