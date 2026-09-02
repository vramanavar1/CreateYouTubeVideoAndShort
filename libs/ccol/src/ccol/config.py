"""Configuration for the observability layer.

Pure data. This module deliberately never reads ``os.environ``: the consuming
project owns environment parsing, which is what keeps ``ccol`` reusable and keeps
the host project's "no module reads os.environ directly" rule intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str
    service_version: str = ""
    environment: str = "local"
    level: str = "INFO"
    #: "console" for a human watching a run, "json" for a log shipper.
    console_format: str = "console"
    log_file: Path | None = None
    #: Empty means stdout only. This single field is the whole degradation switch:
    #: without it no exporter is created and no telemetry package is imported.
    connection_string: str = ""
    enabled: bool = True
    #: Third-party loggers to pin at WARNING because they drown out our own lines.
    noisy_loggers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def azure_enabled(self) -> bool:
        return self.enabled and bool(self.connection_string)
