"""The sink contract.

A sink takes a published job and delivers its short URL somewhere. Two rules keep
fan-out safe to retry:

* A sink is handed the job's ``delivery_id`` for itself -- derived from
  ``sha256(job_id + sink_name)``, so it is stable across runs and unique per
  target. A sink that talks to an idempotency-aware API should pass it along.
* A sink raising is *not* a pipeline failure. The registry catches it and records
  a failed ``SinkResult``; the video is already live, and failing the job over an
  undelivered notification would be the wrong trade.
"""

from __future__ import annotations

from typing import Protocol

from ytshort.contracts.models import Job, SinkResult
from ytshort.pipeline.stage import PipelineContext


class Sink(Protocol):
    name: str

    def deliver(self, job: Job, ctx: PipelineContext) -> SinkResult: ...


def message_for(job: Job) -> tuple[str, str]:
    """The subject and plain-text body every sink shares."""
    assert job.publication is not None
    publication = job.publication

    subject = f"Short published: {job.title}"[:150]
    lines = [
        job.title,
        "",
        publication.short_url,
        "",
        f"Visibility: {publication.privacy_status}",
        f"Source email: {job.source.subject or '(no subject)'}",
    ]
    if job.media.duration_seconds:
        lines.append(f"Duration: {job.media.duration_seconds:.1f}s")
    if job.media.audio_track:
        lines.append(f"Background track: {job.media.audio_track}")

    warnings = job.warn_findings
    if warnings:
        lines += ["", "Review warnings that were accepted:"]
        lines += [f"  - [{f.kind}] {f.detail}" for f in warnings]

    if publication.privacy_status == "private":
        lines += [
            "",
            "Note: this upload is private. YouTube locks uploads from API projects "
            "that have not passed its compliance audit to private, so the link will "
            "only work while signed in as the channel owner.",
        ]

    return subject, "\n".join(lines)
