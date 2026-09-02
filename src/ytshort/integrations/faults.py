"""Is this fault worth retrying?

Retrying everything is not caution, it is a self-inflicted denial of service: a
revoked credential or an exhausted quota returns the same 403 forever, and the
pipeline would ask again on every scheduled run. Classify, then retry only what
can actually succeed next time.

The default for an unrecognised exception is **permanent**. A ``KeyError`` in our
own code is a bug, and a bug retried on a timer is a bug you find out about from
the bill rather than from the logs.
"""

from __future__ import annotations

#: 408 request timeout, 429 rate limited, and the 5xx family. Everything else in
#: the 4xx range is the caller's fault and will not fix itself.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def http_status(exc: BaseException) -> int | None:
    """Pull an HTTP status out of whichever client raised.

    googleapiclient's ``HttpError`` carries ``status_code`` and ``resp.status``;
    urllib's ``HTTPError`` carries ``code``.
    """
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value

    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int):
        return status
    # httplib2 hands back a dict-like response rather than an object.
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


def is_transient(exc: BaseException) -> bool:
    status = http_status(exc)
    if status is not None:
        return status in _TRANSIENT_STATUS

    # No status at all: a socket timeout, a reset connection, a DNS failure. These
    # are the textbook transient faults and they are worth another attempt.
    return isinstance(exc, TimeoutError | ConnectionError)
