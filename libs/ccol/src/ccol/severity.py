"""Severity vocabulary.

Deliberately a small closed set rather than raw ``logging`` integers: a calling
project maps its own domain severity onto this, and the mapping is then the only
place that has to know about ``logging`` levels at all.
"""

from __future__ import annotations

import logging
from enum import StrEnum


class Level(StrEnum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


_LOG_LEVELS: dict[Level, int] = {
    Level.debug: logging.DEBUG,
    Level.info: logging.INFO,
    Level.warning: logging.WARNING,
    Level.error: logging.ERROR,
    Level.critical: logging.CRITICAL,
}


def to_log_level(level: Level) -> int:
    # .get rather than [] so an unmapped member degrades to a visible record
    # instead of raising inside a caller's exception guard.
    return _LOG_LEVELS.get(level, logging.WARNING)
