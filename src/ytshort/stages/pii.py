"""PII screening across everything that will become public.

Three surfaces get screened, and the second is the one that is easy to forget:

1. **Pixels** -- text visible in the image, via OCR (optional; skipped cleanly
   when tesseract is not installed).
2. **The email subject** -- it becomes the video title *and* is burned into the
   thumbnail. A subject like "Invoice for +44 7700 900123" publishes a phone
   number in 60-point type.
3. **The email body snippet** -- it seeds the video description.

Policy lives here, detection lives in ``detectors.py``. ``YTSHORT_PII_POLICY``
chooses between ``warn`` (surface it, let the reviewer decide) and ``block``
(quarantine before a human ever sees it). What the stage always reports is what
it could *not* check -- a job where OCR never ran says so, rather than looking
identical to a job that was checked and came back clean.
"""

from __future__ import annotations

from pathlib import Path

from ytshort.contracts.models import Finding, Job, Severity
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline
from ytshort.pipeline.stage import BaseStage, PipelineContext
from ytshort.stages.detectors import Detection, detect

log = get_logger(__name__)


def _ocr(path: Path) -> tuple[str, str | None]:
    """Return (text, skip_reason). ``skip_reason`` is None when OCR actually ran."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", "pytesseract is not installed (uv sync --extra ocr)"

    try:
        with Image.open(path) as image:
            return pytesseract.image_to_string(image), None
    except pytesseract.TesseractNotFoundError:
        return "", "the tesseract binary is not on PATH"
    except Exception as exc:  # noqa: BLE001 - OCR must never fail a job
        return "", f"OCR failed ({exc})"


class PiiStage(BaseStage):
    name = "pii"
    # No success_state: screening annotates the job, it does not advance it past
    # what SafetyStage already established.
    success_state = None

    def run(self, job: Job, ctx: PipelineContext) -> None:
        policy_severity = (
            Severity.blocking if ctx.settings.pii_policy == "block" else Severity.warn
        )

        self._screen_text(job, job.source.subject, "email.subject", policy_severity)
        self._screen_text(job, job.source.body_snippet, "email.body", policy_severity)

        for attachment in job.media_attachments:
            if attachment.kind != "image" or attachment.stored_path is None:
                continue
            path = ctx.media_store.resolve(job.job_id, attachment.stored_path)
            text, skip_reason = _ocr(path)

            if skip_reason is not None:
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="pii.not_screened",
                        severity=Severity.warn,
                        where=attachment.filename,
                        detail=f"image text was not read: {skip_reason}",
                        action_taken="screening skipped",
                    )
                )
                continue

            self._screen_text(job, text, attachment.filename, policy_severity)

        # Videos are not OCR'd frame by frame -- say so rather than let their
        # absence from the findings read as "checked and clean".
        for attachment in job.media_attachments:
            if attachment.kind == "video":
                job.add_finding(
                    Finding(
                        stage=self.name,
                        kind="pii.not_screened",
                        severity=Severity.info,
                        where=attachment.filename,
                        detail="video frames are not OCR-screened for PII in this version",
                        action_taken="screening skipped",
                    )
                )

        if job.blocking_findings:
            kinds = ", ".join(sorted({f.kind for f in job.blocking_findings}))
            raise HaltPipeline(f"blocked by PII policy: {kinds}")

    def _screen_text(
        self, job: Job, text: str, where: str, policy_severity: Severity
    ) -> None:
        for detection in detect(text):
            job.add_finding(self._to_finding(detection, where, policy_severity))
            log.info(
                "pii detected",
                extra={"kind": detection.kind, "where": where, "value": detection.redacted},
            )

    def _to_finding(
        self, detection: Detection, where: str, policy_severity: Severity
    ) -> Finding:
        # A medium-confidence hit (an unvalidated number that merely looks like a
        # phone) is never allowed to hard-block -- that would quarantine jobs over
        # a date or an order number. It still reaches the reviewer as a warning.
        severity = policy_severity if detection.confidence == "high" else Severity.warn
        return Finding(
            stage=self.name,
            kind=f"pii.{detection.kind}",
            severity=severity,
            where=where,
            detail=(
                f"possible {detection.kind.replace('_', ' ')} "
                f"({detection.confidence} confidence): {detection.redacted}"
            ),
            action_taken=(
                "quarantined" if severity is Severity.blocking else "flagged for review"
            ),
        )
