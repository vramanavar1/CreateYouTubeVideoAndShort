"""Produce the short URL that gets distributed.

YouTube already mints one -- ``https://youtu.be/<id>`` -- so v1 uses it rather
than adding a third-party dependency, an account, and another thing that can be
down. The ``Shortener`` seam exists so a branded domain or Bitly can be dropped in
without touching the distribute stage.
"""

from __future__ import annotations

from typing import Protocol

from ytshort.contracts.models import Job
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)


class Shortener(Protocol):
    name: str

    def shorten(self, url: str) -> str: ...


class YouTubeCanonicalShortener:
    """Uses the youtu.be link YouTube already provides. No network call."""

    name = "youtu.be"

    def shorten(self, url: str) -> str:
        return url


class ShortenStage(BaseStage):
    name = "shorten"
    success_state = None

    def __init__(self, shortener: Shortener | None = None) -> None:
        self.shortener = shortener or YouTubeCanonicalShortener()

    def run(self, job: Job, ctx: PipelineContext) -> None:
        if job.publication is None:
            raise HaltPipeline("shorten ran before the video was published")

        job.publication.short_url = self.shortener.shorten(job.publication.short_url)
        log.info("short url ready", extra={"url": job.publication.short_url})
