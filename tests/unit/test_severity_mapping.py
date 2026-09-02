"""Domain severity -> telemetry severity, and the finding observer hook.

The hook is the mechanism behind "findings are never dropped": every Finding in
the pipeline goes through Job.add_finding, so a test here covers all of them.
"""

from __future__ import annotations

import logging

import pytest
from ccol import Level, to_log_level

from ytshort.contracts.models import (
    Finding,
    Job,
    Severity,
    SourceEmail,
    _observers,
    observe_findings,
)
from ytshort.observability.severity import DOMAIN_TO_LEVEL, level_for


@pytest.fixture
def job() -> Job:
    return Job(job_id="j1", source=SourceEmail(message_id="m1", subject="s"))


@pytest.fixture
def observers_restored():
    """The observer list is process-global; put it back after each test."""
    original = list(_observers)
    yield
    _observers[:] = original


def finding(severity: Severity = Severity.warn, kind: str = "pii.email") -> Finding:
    return Finding(
        stage="pii", kind=kind, severity=severity, where="a.png", detail="d"
    )


class TestSeverityMapping:
    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            (Severity.info, logging.INFO),
            (Severity.warn, logging.WARNING),
            # A blocking finding quarantines the job. Before this it produced no
            # ERROR record at all, so no severity-based alert could ever see it.
            (Severity.blocking, logging.ERROR),
        ],
    )
    def test_domain_severity_maps_to_a_log_level(
        self, severity: Severity, expected: int
    ) -> None:
        assert to_log_level(level_for(severity)) == expected

    def test_every_domain_severity_is_mapped(self) -> None:
        # Catches a future fourth Severity member. Without the mapping being total,
        # an unmapped one would raise inside the observer's exception guard and the
        # event would vanish silently.
        assert set(DOMAIN_TO_LEVEL) == set(Severity)

    def test_an_unmapped_severity_degrades_rather_than_raising(self) -> None:
        assert level_for("not-a-severity") is Level.warning  # type: ignore[arg-type]


class TestFindingObservers:
    def test_every_finding_is_emitted_exactly_once(self, job, observers_restored) -> None:
        seen: list[Finding] = []
        observe_findings(lambda _job, f: seen.append(f))

        job.add_finding(finding(Severity.info, "malware.clean"))
        job.add_finding(finding(Severity.warn, "pii.email"))
        job.add_finding(finding(Severity.blocking, "malware.detected"))

        assert [f.kind for f in seen] == ["malware.clean", "pii.email", "malware.detected"]
        assert len(job.findings) == 3

    def test_a_raising_observer_never_loses_a_finding(self, job, observers_restored) -> None:
        # The load-bearing one. A telemetry fault must not cost us the record: the
        # append happens before observers are notified, so it cannot be undone.
        def explode(_job: Job, _finding: Finding) -> None:
            raise RuntimeError("telemetry is down")

        observe_findings(explode)
        recorded = job.add_finding(finding(Severity.blocking))

        assert job.findings == [recorded]
        assert job.blocking_findings == [recorded]

    def test_a_raising_observer_does_not_stop_the_next_one(
        self, job, observers_restored
    ) -> None:
        seen: list[Finding] = []

        def explode(_job: Job, _finding: Finding) -> None:
            raise RuntimeError("boom")

        observe_findings(explode)
        observe_findings(lambda _job, f: seen.append(f))
        job.add_finding(finding())

        assert len(seen) == 1

    def test_findings_that_record_a_skipped_layer_are_emitted_too(
        self, job, observers_restored
    ) -> None:
        # A job that was not scanned must never look like one that was scanned and
        # came back clean -- in telemetry as much as on the record.
        seen: list[Finding] = []
        observe_findings(lambda _job, f: seen.append(f))
        job.add_finding(finding(Severity.warn, "malware.not_scanned"))

        assert [f.kind for f in seen] == ["malware.not_scanned"]
