"""The facade a consuming project holds.

One object exposes logging, events, metrics and spans. When nothing is configured
-- a laptop run, a test -- every method still works and simply does not export:
loggers write to stdout as before, instruments are no-ops, spans are inert. Call
sites therefore never ask whether telemetry is on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from ccol import _azure
from ccol.config import ObservabilityConfig
from ccol.logging import get_logger, setup_logging
from ccol.metrics import Instrument, NoopInstrument
from ccol.severity import Level, to_log_level

_NOOP = NoopInstrument()


class Observability:
    def __init__(self, config: ObservabilityConfig, *, azure_enabled: bool) -> None:
        self.config = config
        self.azure_enabled = azure_enabled
        self._instruments: dict[str, Instrument] = {}
        self._log = get_logger(f"{config.service_name}.observability")

    # -- logging -----------------------------------------------------------
    def logger(self, name: str) -> logging.Logger:
        return get_logger(name)

    def event(self, message: str, *, level: Level = Level.info, **context: object) -> None:
        """Emit one structured event. ``context`` becomes customDimensions."""
        self._log.log(to_log_level(level), message, extra=context)

    # -- metrics -----------------------------------------------------------
    def counter(self, name: str, *, unit: str = "1", description: str = "") -> Instrument:
        return self._instrument("counter", name, unit, description)

    def histogram(self, name: str, *, unit: str = "ms", description: str = "") -> Instrument:
        return self._instrument("histogram", name, unit, description)

    def _instrument(self, kind: str, name: str, unit: str, description: str) -> Instrument:
        cached = self._instruments.get(name)
        if cached is not None:
            return cached
        made: Instrument | None = None
        if self.azure_enabled:
            factory = _azure.make_counter if kind == "counter" else _azure.make_histogram
            made = factory(self.config.service_name, name, unit, description)
        instrument = made or _NOOP
        self._instruments[name] = instrument
        return instrument

    # -- tracing -----------------------------------------------------------
    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[object | None]:
        tracer = _azure.tracer(self.config.service_name) if self.azure_enabled else None
        if tracer is None:
            yield None
            return
        with tracer.start_as_current_span(name) as current_span:  # type: ignore[attr-defined]
            for key, value in attributes.items():
                current_span.set_attribute(key, value)
            yield current_span

    def flush(self, timeout_ms: int = 5000) -> None:
        if self.azure_enabled:
            _azure.flush(timeout_ms)


#: Set by configure(). Until then current() hands back an inert instance so that
#: importing a module which grabs an instrument at import time cannot explode.
_current: Observability | None = None


def configure(config: ObservabilityConfig) -> Observability:
    """Configure logging and, when a connection string is set, Azure export.

    Idempotent: calling it again reconfigures rather than stacking handlers.
    """
    global _current

    setup_logging(
        level=config.level,
        fmt=config.console_format,
        log_file=config.log_file,
        service_prefix=config.service_name,
        noisy_loggers=config.noisy_loggers,
    )
    azure_enabled = _azure.try_configure(config)
    _current = Observability(config, azure_enabled=azure_enabled)
    return _current


def current() -> Observability:
    if _current is None:
        return Observability(ObservabilityConfig(service_name="ccol"), azure_enabled=False)
    return _current
