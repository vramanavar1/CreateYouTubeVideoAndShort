"""The job record: one JSON document that is the single source of truth.

Every stage reads and mutates a ``Job``; the runner persists it after each stage.
That is what makes runs resumable and idempotent -- the record on disk always
says exactly how far a job got and why it stopped there.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_job_id(gmail_message_id: str) -> str:
    """Stable job id derived from the Gmail message id.

    This is the backbone of idempotency: the same email always maps to the same
    job, so re-running ingestion can never create a duplicate.
    """
    return hashlib.sha256(gmail_message_id.encode("utf-8")).hexdigest()[:32]


class Severity(StrEnum):
    info = "info"
    warn = "warn"
    blocking = "blocking"


class JobState(StrEnum):
    discovered = "discovered"
    ingested = "ingested"
    screened = "screened"
    composed = "composed"
    awaiting_review = "awaiting_review"
    approved = "approved"
    rejected = "rejected"
    published = "published"
    distributed = "distributed"
    done = "done"
    quarantined = "quarantined"
    failed = "failed"


#: States from which no further automated work happens without a human.
TERMINAL_STATES = frozenset(
    {JobState.done, JobState.rejected, JobState.quarantined, JobState.failed}
)


class StageStatus(StrEnum):
    completed = "completed"
    suspended = "suspended"
    halted = "halted"
    failed = "failed"


class Finding(BaseModel):
    """One thing the screening stages noticed. Findings are never silently dropped.

    ``severity`` drives policy: a single ``blocking`` finding quarantines the job
    and it never reaches the reviewer as approvable. ``warn`` findings are shown
    in the review UI so the human decides with full information.
    """

    stage: str
    kind: str
    severity: Severity
    where: str
    detail: str
    action_taken: str = "none"
    detected_at: datetime = Field(default_factory=utcnow)


class Attachment(BaseModel):
    attachment_id: str
    filename: str
    declared_mime: str
    size_bytes: int
    kind: Literal["image", "video", "other"] = "other"
    sha256: str | None = None
    stored_path: str | None = None
    detected_mime: str | None = None
    accepted: bool = True
    reject_reason: str | None = None

    @property
    def is_media(self) -> bool:
        return self.accepted and self.kind in ("image", "video")


class SourceEmail(BaseModel):
    message_id: str
    thread_id: str = ""
    sender: str = ""
    subject: str = ""
    body_snippet: str = ""
    received_at: datetime | None = None


class MediaArtifacts(BaseModel):
    """Paths to everything rendered for this job, relative to the media dir."""

    primary_image: str | None = None
    primary_video: str | None = None
    thumbnail_tall: str | None = None  # 1080x1920, used as the video bumper
    thumbnail_wide: str | None = None  # 1280x720, used for thumbnails.set
    audio_track: str | None = None
    composed_video: str | None = None
    duration_seconds: float | None = None


class Review(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer: str = "local"
    reason: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


class Publication(BaseModel):
    video_id: str
    watch_url: str
    short_url: str
    privacy_status: str
    thumbnail_set: bool = False
    published_at: datetime = Field(default_factory=utcnow)


class SinkResult(BaseModel):
    sink: str
    delivery_id: str
    ok: bool
    detail: str = ""
    delivered_at: datetime = Field(default_factory=utcnow)


class StageRecord(BaseModel):
    name: str
    status: StageStatus
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    detail: str = ""


class Job(BaseModel):
    job_id: str
    state: JobState = JobState.discovered
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    source: SourceEmail
    attachments: list[Attachment] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    media: MediaArtifacts = Field(default_factory=MediaArtifacts)

    # Editable in the review UI before publishing.
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    review: Review | None = None
    publication: Publication | None = None
    deliveries: list[SinkResult] = Field(default_factory=list)
    error: str | None = None

    # -- helpers -----------------------------------------------------------
    def add_finding(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        return finding

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.blocking]

    @property
    def warn_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.warn]

    @property
    def media_attachments(self) -> list[Attachment]:
        return [a for a in self.attachments if a.is_media]

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def stage_completed(self, name: str) -> bool:
        record = self.stages.get(name)
        return record is not None and record.status is StageStatus.completed

    def delivery_id_for(self, sink_name: str) -> str:
        """Derived, stable per (job, sink) so redelivery is idempotent."""
        return hashlib.sha256(f"{self.job_id}:{sink_name}".encode()).hexdigest()[:24]

    def delivered(self, sink_name: str) -> bool:
        return any(d.sink == sink_name and d.ok for d in self.deliveries)
