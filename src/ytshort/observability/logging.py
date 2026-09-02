"""Structured logging with a per-job correlation ID.

The whole point of this module is that one job's entire lifecycle -- ingest,
screening, render, review, publish, fan-out -- can be pulled out of a mixed log
stream with a single grep on ``correlation_id``. A ``contextvar`` carries the id
so call sites never have to thread it through their signatures.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

# Attributes present on every LogRecord; anything else a caller attaches via
# ``extra=`` is treated as structured context and emitted alongside the message.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def correlation_id() -> str:
    return _correlation_id.get()


@contextmanager
def use_job(job_id: str) -> Iterator[None]:
    """Bind a job id to every log record emitted inside the block."""
    token = _correlation_id.set(job_id)
    try:
        yield
    finally:
        _correlation_id.reset(token)


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
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
            "correlation_id": getattr(record, "correlation_id", "-"),
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable variant for interactive CLI runs."""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", "-")
        # Short-form the job id: the first 8 chars are plenty to eyeball, and the
        # full value is always available in the JSON sink.
        cid_short = cid[:8] if cid != "-" else "-"
        head = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"[{cid_short}] {record.name.removeprefix('ytshort.')}: {record.getMessage()}"
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
) -> None:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
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

    # googleapiclient is extremely chatty at INFO and drowns out our own lines.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
