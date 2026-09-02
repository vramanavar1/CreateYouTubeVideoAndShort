"""Build the configured sink list.

The file sink is always present, even when it is not in ``YTSHORT_SINKS``. It
costs nothing, cannot fail for network reasons, and guarantees every published
job leaves a durable record of where its link went.
"""

from __future__ import annotations

from ytshort.observability.logging import get_logger
from ytshort.sinks.base import Sink
from ytshort.sinks.email_sink import EmailSink
from ytshort.sinks.file_sink import FileSink

log = get_logger(__name__)

_AVAILABLE = {
    "file": FileSink,
    "email": EmailSink,
}


def build_sinks(names: tuple[str, ...] | list[str]) -> list[Sink]:
    sinks: list[Sink] = [FileSink()]
    seen = {"file"}

    for name in names:
        key = name.strip().lower()
        if key in seen:
            continue
        factory = _AVAILABLE.get(key)
        if factory is None:
            log.warning("unknown sink in configuration, ignoring", extra={"sink": key})
            continue
        sinks.append(factory())
        seen.add(key)

    return sinks
