"""The pipeline stages, in execution order."""

from ytshort.stages.compose import ComposeStage
from ytshort.stages.distribute import DistributeStage
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.stages.pii import PiiStage
from ytshort.stages.publish import PublishStage
from ytshort.stages.review import ReviewGateStage
from ytshort.stages.safety import SafetyStage
from ytshort.stages.shorten import ShortenStage
from ytshort.stages.thumbnail import ThumbnailStage

__all__ = [
    "ComposeStage",
    "DistributeStage",
    "IngestStage",
    "PiiStage",
    "PublishStage",
    "ReviewGateStage",
    "SafetyStage",
    "ShortenStage",
    "ThumbnailStage",
    "build_stages",
    "discover_jobs",
]


def build_stages() -> list:
    """The canonical stage order. The runner skips whatever is already done."""
    return [
        IngestStage(),
        SafetyStage(),
        PiiStage(),
        ThumbnailStage(),
        ComposeStage(),
        ReviewGateStage(),
        PublishStage(),
        ShortenStage(),
        DistributeStage(),
    ]
