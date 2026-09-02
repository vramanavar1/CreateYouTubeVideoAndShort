"""Emits every recorded finding as a severity-mapped telemetry event.

Registered once against ``Job.add_finding``, which all ~20 ``Finding``
constructions in the pipeline pass through. Hooking the single choke point is
what makes "findings are never dropped" mechanically true of telemetry as well as
of the job record -- including the ``*.not_screened`` findings that exist
precisely to record a layer which could not run.
"""

from __future__ import annotations

from ccol import to_log_level

from ytshort.contracts.models import Finding, Job
from ytshort.observability import instruments
from ytshort.observability.logging import get_logger
from ytshort.observability.severity import level_for

log = get_logger(__name__)

#: Findings quote attacker-influenced text (filenames, subjects, OCR output).
#: PII detail is already redacted upstream; this bounds the rest.
_MAX_DETAIL = 400


def emit_finding(job: Job, finding: Finding) -> None:
    log.log(
        to_log_level(level_for(finding.severity)),
        "finding recorded",
        extra={
            "event": "finding",
            "job": job.job_id,
            "job_state": job.state.value,
            "stage": finding.stage,
            "kind": finding.kind,
            "severity": finding.severity.value,
            # Attacker-supplied. A log field only -- never a metric attribute.
            "where": finding.where,
            "detail": finding.detail[:_MAX_DETAIL],
            "action_taken": finding.action_taken,
        },
    )
    instruments.findings().add(
        1,
        {
            "severity": finding.severity.value,
            "stage": finding.stage,
            "kind": finding.kind,
        },
    )
