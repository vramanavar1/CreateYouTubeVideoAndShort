"""Ambient correlation id.

A ``contextvar`` carries the id so call sites never have to thread it through
their signatures. Binding nests: an outer scope (a CLI invocation, an HTTP
request) can be overridden by an inner one (a single job) and is restored on the
way out.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: What an unbound log record reports. A literal "-" is easier to spot in a log
#: stream -- and to grep for -- than an empty field.
UNSET = "-"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default=UNSET)


def correlation_id() -> str:
    return _correlation_id.get()


def new_correlation_id() -> str:
    """A fresh id for work that did not arrive with one."""
    return uuid.uuid4().hex[:32]


@contextmanager
def use_correlation(value: str) -> Iterator[None]:
    """Bind ``value`` to every log record emitted inside the block."""
    token = _correlation_id.set(value or UNSET)
    try:
        yield
    finally:
        _correlation_id.reset(token)
