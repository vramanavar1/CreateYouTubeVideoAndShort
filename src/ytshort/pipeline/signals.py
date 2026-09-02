"""Control-flow signals a stage raises to steer the runner.

Three, and only three, ways for a stage to stop the pipeline. Anything else that
escapes a stage is an unexpected bug and lands the job in ``failed`` with the
traceback recorded -- which is exactly what you want, because a silent
half-processed job is worse than a loud broken one.
"""

from __future__ import annotations

from ytshort.contracts.models import JobState


class PipelineSignal(Exception):
    """Base for the deliberate, non-bug ways a stage can end a run."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HaltPipeline(PipelineSignal):
    """Terminal stop -- the job must not proceed. Used by the safety gates.

    ``state`` says which terminal state to land in; the default is
    ``quarantined`` because the overwhelming majority of halts come from the
    security/PII screen.
    """

    def __init__(self, reason: str, state: JobState = JobState.quarantined) -> None:
        super().__init__(reason)
        self.state = state


class SuspendPipeline(PipelineSignal):
    """Park the job and wait for something outside the process -- i.e. a human."""

    def __init__(self, reason: str, state: JobState = JobState.awaiting_review) -> None:
        super().__init__(reason)
        self.state = state


class RetryableFailure(PipelineSignal):
    """A transient fault. The stage is not marked complete, so a re-run retries it.

    ``retry_after_seconds`` is a floor, not a schedule: the runner backs off
    exponentially on its own and will wait at least this long when a service has
    told us how long to wait (a ``Retry-After`` header, say). Retries are bounded
    -- see ``PipelineRunner``, which dead-letters a stage that keeps failing rather
    than asking a broken service forever.
    """

    def __init__(self, reason: str, retry_after_seconds: float = 0.0) -> None:
        super().__init__(reason)
        self.retry_after_seconds = retry_after_seconds
