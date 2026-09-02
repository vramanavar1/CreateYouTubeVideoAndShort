"""Metric instruments, with a no-op that is always safe to call.

Call sites never branch on whether telemetry is configured -- an unconfigured
process hands back a ``NoopInstrument`` whose methods do nothing. That is what
keeps instrumentation to one line at each emitting site.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Instrument(Protocol):
    def add(self, value: int | float, attributes: dict[str, object] | None = None) -> None: ...

    def record(self, value: int | float, attributes: dict[str, object] | None = None) -> None: ...


class NoopInstrument:
    def add(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        return None

    def record(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        return None


class _OtelCounter:
    def __init__(self, counter: object) -> None:
        self._counter = counter

    def add(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        self._counter.add(value, attributes or {})  # type: ignore[attr-defined]

    def record(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        self.add(value, attributes)


class _OtelHistogram:
    def __init__(self, histogram: object) -> None:
        self._histogram = histogram

    def record(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        self._histogram.record(value, attributes or {})  # type: ignore[attr-defined]

    def add(self, value: int | float, attributes: dict[str, object] | None = None) -> None:
        self.record(value, attributes)
