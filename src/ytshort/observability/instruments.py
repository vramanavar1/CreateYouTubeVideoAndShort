"""Metric instruments, named in one place.

Each accessor resolves through ``ccol.current()`` rather than caching a module
global, so instruments follow whatever ``configure()`` most recently produced --
which matters for tests, and for the fact that ``bootstrap()`` runs after import.
``Observability`` caches by name, so repeated calls are cheap.

**Cardinality rule.** Only bounded values may be attributes. ``finding.kind`` is a
closed set of literals and is safe; ``finding.where`` is an attacker-supplied
filename and must stay a log field. An unbounded attribute mints unbounded time
series, which is a billing incident a hostile sender can trigger for free.
"""

from __future__ import annotations

from ccol import Instrument, current


def stage_duration() -> Instrument:
    return current().histogram(
        "ytshort.stage.duration", unit="ms", description="Wall time per pipeline stage"
    )


def findings() -> Instrument:
    return current().counter(
        "ytshort.findings", description="Screening findings by severity, stage and kind"
    )


def job_state_transitions() -> Instrument:
    return current().counter(
        "ytshort.job.state_transitions", description="Job state changes"
    )


def emails_ingested() -> Instrument:
    return current().counter(
        "ytshort.emails.ingested", description="Emails turned into jobs (the daily cap)"
    )


def publish_outcomes() -> Instrument:
    return current().counter(
        "ytshort.publish.outcomes", description="YouTube uploads by applied privacy"
    )


def sink_deliveries() -> Instrument:
    return current().counter(
        "ytshort.sink.deliveries", description="Fan-out attempts per sink"
    )


def security_events() -> Instrument:
    """One counter with a bounded ``event`` attribute, rather than one per event.

    Keeps the metric surface small while still making CSRF rejections, sender
    allow-list rejections and review decisions alertable.
    """
    return current().counter(
        "ytshort.security.events", description="Security-relevant events by kind"
    )


def job_trigger_requests() -> Instrument:
    return current().counter(
        "ytshort.job_trigger.requests", description="ARM calls starting the scheduled job"
    )
