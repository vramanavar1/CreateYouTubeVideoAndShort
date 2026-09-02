"""The only module that touches Azure Monitor / OpenTelemetry, and only lazily.

Every import here sits inside a function. An unconfigured process therefore never
imports ``opentelemetry`` or ``azure.monitor`` at all -- which is what makes an
offline test suite structurally safe rather than merely well-behaved, and is
asserted directly by ``test_telemetry_offline.py``.

Nothing in here may raise. Telemetry that cannot start must leave the application
running on stdout logging, not take it down.
"""

from __future__ import annotations

import logging

from ccol.config import ObservabilityConfig
from ccol.metrics import Instrument, _OtelCounter, _OtelHistogram

log = logging.getLogger(__name__)


def try_configure(config: ObservabilityConfig) -> bool:
    """Wire Azure Monitor export. Returns True only if it actually started."""
    if not config.azure_enabled:
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        log.warning(
            "a connection string is set but the observability extra is not installed; "
            "continuing with stdout logging only"
        )
        return False

    resource_attributes = {
        "service.name": config.service_name,
        "service.namespace": config.environment,
    }
    if config.service_version:
        resource_attributes["service.version"] = config.service_version

    try:
        configure_azure_monitor(
            connection_string=config.connection_string,
            resource_attributes=resource_attributes,
            # Instrumentors we have no use for. Left on: fastapi (the review UI's
            # request spans) and urllib (the ARM trigger call).
            instrumentation_options={
                "azure_sdk": {"enabled": False},
                "django": {"enabled": False},
                "flask": {"enabled": False},
                "psycopg2": {"enabled": False},
            },
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail the app
        log.warning("azure monitor export could not start", extra={"error": repr(exc)})
        return False

    return True


def _meter(name: str) -> object | None:
    try:
        from opentelemetry import metrics
    except ImportError:
        return None
    return metrics.get_meter(name)


def tracer(scope: str) -> object | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer(scope)


def make_counter(scope: str, name: str, unit: str, description: str) -> Instrument | None:
    meter = _meter(scope)
    if meter is None:
        return None
    try:
        return _OtelCounter(
            meter.create_counter(name, unit=unit, description=description)  # type: ignore[attr-defined]
        )
    except Exception:  # noqa: BLE001 - an instrument is never worth an outage
        return None


def make_histogram(scope: str, name: str, unit: str, description: str) -> Instrument | None:
    meter = _meter(scope)
    if meter is None:
        return None
    try:
        return _OtelHistogram(
            meter.create_histogram(name, unit=unit, description=description)  # type: ignore[attr-defined]
        )
    except Exception:  # noqa: BLE001
        return None


def flush(timeout_ms: int) -> None:
    """Force-flush pending telemetry. Used before a short-lived process exits."""
    try:
        from opentelemetry import metrics, trace
    except ImportError:
        return
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is None:
            continue
        try:
            force_flush(timeout_ms)
        except Exception:  # noqa: BLE001
            continue
