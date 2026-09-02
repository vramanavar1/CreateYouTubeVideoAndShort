"""Maps this project's domain severity onto the telemetry vocabulary.

The vocabulary belongs to ``ccol``; the mapping belongs here, because what a
``blocking`` finding *means* operationally is a ytshort question.
"""

from __future__ import annotations

from ccol.severity import Level

from ytshort.contracts.models import Severity

#: ``blocking`` is ERROR rather than CRITICAL. A blocking finding quarantines the
#: job and requires a human -- operationally an error, even though the screening
#: itself worked correctly. CRITICAL is reserved for "the pipeline is broken",
#: which is the runner's catch-all. Keeping that distinction is what makes an
#: alert rule on CRITICAL meaningful.
DOMAIN_TO_LEVEL: dict[Severity, Level] = {
    Severity.info: Level.info,
    Severity.warn: Level.warning,
    Severity.blocking: Level.error,
}


def level_for(severity: Severity) -> Level:
    # .get, not [] -- a future fourth Severity member would otherwise raise inside
    # the observer's exception guard and silently lose the event. Degrading to a
    # warning keeps it visible. test_severity_mapping asserts the dict is total.
    return DOMAIN_TO_LEVEL.get(severity, Level.warning)
