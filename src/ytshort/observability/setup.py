"""Builds the CCOL configuration from Settings and wires it up.

This is the seam between the project and the reusable layer: ``Settings`` knows
the environment, ``ccol`` knows telemetry, and neither imports the other.
"""

from __future__ import annotations

from ccol import Observability, ObservabilityConfig
from ccol import configure as configure_ccol

from ytshort.config import Settings
from ytshort.contracts.models import observe_findings
from ytshort.observability.findings import emit_finding
from ytshort.observability.logging import NOISY_LOGGERS, get_logger

log = get_logger(__name__)

_findings_registered = False


def observability_config(settings: Settings) -> ObservabilityConfig:
    return ObservabilityConfig(
        service_name=settings.service_name,
        service_version=settings.service_version,
        environment=settings.environment_name,
        level=settings.log_level,
        console_format=settings.log_format,
        # In Azure the container's stderr is already collected into Log Analytics,
        # so a second copy on an SMB share is pure churn.
        log_file=settings.logs_dir / "ytshort.jsonl" if settings.log_to_file else None,
        connection_string=settings.otel_connection_string,
        enabled=settings.otel_enabled,
        noisy_loggers=NOISY_LOGGERS,
    )


def configure_observability(settings: Settings) -> Observability:
    """Configure logging and, when configured, Azure Monitor export."""
    global _findings_registered

    obs = configure_ccol(observability_config(settings))

    # Registered once per process: add_finding notifies every observer, so a
    # second registration would double-count the findings metric.
    if not _findings_registered:
        observe_findings(emit_finding)
        _findings_registered = True

    if obs.azure_enabled:
        # Says *that* export started, never the connection string.
        log.info(
            "telemetry export enabled",
            extra={
                "service": settings.service_name,
                "environment": settings.environment_name,
                "job_execution": settings.job_execution_name,
            },
        )
    return obs
