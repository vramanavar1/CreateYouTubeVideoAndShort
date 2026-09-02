"""Structured logging carrying a correlation id.

The whole point of this module is that one unit of work's entire lifecycle can be
pulled out of a mixed log stream with a single grep on ``correlation_id``.

Lifted from ytshort's ``observability.logging`` with one generalisation: the
logger-name prefix trimmed for console output is configured rather than hardcoded.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from ccol.context import UNSET, correlation_id

# Attributes present on every LogRecord; anything else a caller attaches via
# ``extra=`` is treated as structured context and emitted alongside the message.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id()
        return True


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED and key != "correlation_id"
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line -- what App Insights / any log shipper wants."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", UNSET),
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable variant for interactive CLI runs."""

    def __init__(self, service_prefix: str = "") -> None:
        super().__init__()
        # Trimmed from logger names so the console shows "stages.ingest" rather
        # than "ytshort.stages.ingest" on every line.
        self._prefix = f"{service_prefix}." if service_prefix else ""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", UNSET)
        # Short-form the id: the first 8 chars are plenty to eyeball, and the
        # full value is always available in the JSON sink.
        cid_short = cid[:8] if cid != UNSET else UNSET
        name = record.name.removeprefix(self._prefix) if self._prefix else record.name
        head = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"[{cid_short}] {name}: {record.getMessage()}"
        )
        extras = _extras(record)
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            head = f"{head} | {rendered}"
        if record.exc_info:
            head = f"{head}\n{self.formatException(record.exc_info)}"
        return head


def setup_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
    *,
    service_prefix: str = "",
    noisy_loggers: tuple[str, ...] = (),
) -> None:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(
        JsonFormatter() if fmt == "json" else ConsoleFormatter(service_prefix)
    )
    stream.addFilter(_CorrelationFilter())
    root.addHandler(stream)

    # The file sink is always JSON regardless of console format -- the console is
    # for a human watching a run, the file is for grepping afterwards.
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(_CorrelationFilter())
        root.addHandler(file_handler)

    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
