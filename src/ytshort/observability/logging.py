"""Back-compat shim. The implementation now lives in the reusable ``ccol`` package.

27 modules import ``from ytshort.observability.logging import get_logger``. There
is no value in a 27-file rename commit, and keeping this indirection documents
that logging is no longer ytshort-specific -- it is the cross-cutting
observability layer, configured once in ``runtime.bootstrap``.

``use_job`` keeps its historical name here because it reads correctly at the one
call site that matters (the pipeline runner binds a *job* id); ``ccol`` calls the
same thing ``use_correlation`` because it knows nothing about jobs.
"""

from __future__ import annotations

from pathlib import Path

from ccol.context import correlation_id
from ccol.context import use_correlation as use_job
from ccol.logging import ConsoleFormatter, JsonFormatter, get_logger
from ccol.logging import setup_logging as _setup_logging

#: Chatty at INFO, and they drown out our own lines.
NOISY_LOGGERS = ("googleapiclient", "google_auth_oauthlib", "urllib3")

__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "correlation_id",
    "get_logger",
    "setup_logging",
    "use_job",
]


def setup_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
) -> None:
    """Configure the root logger. Safe to call more than once."""
    _setup_logging(
        level=level,
        fmt=fmt,
        log_file=log_file,
        service_prefix="ytshort",
        noisy_loggers=NOISY_LOGGERS,
    )
