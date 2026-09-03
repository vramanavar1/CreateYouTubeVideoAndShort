"""The stage protocol and the context handed to every stage.

A stage is deliberately tiny: a name, an optional state to set on success, and a
``run(job, ctx)``. All shared collaborators arrive through ``PipelineContext``,
which is what lets the tests swap in fake Gmail/YouTube/scanner implementations
and exercise the whole pipeline offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ytshort.config import Settings
from ytshort.contracts.models import Job, JobState
from ytshort.storage.job_store import JobStore
from ytshort.storage.media_store import MediaStore

if TYPE_CHECKING:  # imported lazily to keep the pipeline core free of API deps
    from ytshort.integrations.art_director import ArtDirector
    from ytshort.integrations.gmail_client import GmailClientProtocol
    from ytshort.integrations.moderation import ImageModerator
    from ytshort.integrations.scanner import ScanProvider
    from ytshort.integrations.youtube_client import YouTubeClientProtocol


@dataclass
class PipelineContext:
    settings: Settings
    job_store: JobStore
    media_store: MediaStore
    gmail: GmailClientProtocol | None = None
    youtube: YouTubeClientProtocol | None = None
    scanner: ScanProvider | None = None
    moderator: ImageModerator | None = None
    art_director: ArtDirector | None = None
    # Free-form per-run scratch space; stages use it to pass along values that
    # do not belong on the persisted job record.
    scratch: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Stage(Protocol):
    #: Stable identifier, used as the key in ``Job.stages`` -- renaming one
    #: makes previously completed runs re-execute it, so treat it as an API.
    name: str

    #: State the job moves to when this stage completes. ``None`` leaves the
    #: state alone (used by stages that only annotate the job).
    success_state: JobState | None

    def run(self, job: Job, ctx: PipelineContext) -> None: ...


class BaseStage:
    """Convenience base so concrete stages only implement ``run``."""

    name: str = "unnamed"
    success_state: JobState | None = None

    def run(self, job: Job, ctx: PipelineContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError
