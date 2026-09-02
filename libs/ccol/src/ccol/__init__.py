"""ccol -- a cross-cutting observability layer for AI projects.

Structured logging, correlation ids, severity, metrics and spans, with optional
export to Azure Monitor / Application Insights. The base install has **no
dependencies**: without a connection string it is stdlib logging and nothing
else, and no telemetry package is imported.

    from ccol import ObservabilityConfig, configure, use_correlation

    obs = configure(ObservabilityConfig(service_name="myapp"))
    with use_correlation("job-123"):
        obs.event("work started", stage="ingest")
"""

from ccol.config import ObservabilityConfig
from ccol.context import UNSET, correlation_id, new_correlation_id, use_correlation
from ccol.logging import ConsoleFormatter, JsonFormatter, get_logger, setup_logging
from ccol.metrics import Instrument, NoopInstrument
from ccol.observability import Observability, configure, current
from ccol.severity import Level, to_log_level

__all__ = [
    "UNSET",
    "ConsoleFormatter",
    "Instrument",
    "JsonFormatter",
    "Level",
    "NoopInstrument",
    "Observability",
    "ObservabilityConfig",
    "configure",
    "correlation_id",
    "current",
    "get_logger",
    "new_correlation_id",
    "setup_logging",
    "to_log_level",
    "use_correlation",
]
