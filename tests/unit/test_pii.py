"""PII policy: warn vs block, and honest reporting of what was not screened."""

from __future__ import annotations

import pytest

from ytshort.config import Settings
from ytshort.contracts.models import Severity
from ytshort.pipeline.signals import HaltPipeline
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.stages.pii import PiiStage


def _job_with_subject(ctx, gmail, png_bytes, subject: str, snippet: str = ""):
    gmail.add_message("m1", subject=subject, snippet=snippet, files={"pic.png": png_bytes})
    job = discover_jobs(ctx)[0]
    IngestStage().run(job, ctx)
    return job


def _reload_settings(ctx, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    ctx.settings = Settings.load(env_file=ctx.settings.data_dir / "absent.env")
    ctx.settings.ensure_dirs()


class TestSubjectScreening:
    def test_pii_in_the_subject_is_caught(self, ctx, gmail, png_bytes) -> None:
        # The subject becomes the video title AND is burned into the thumbnail.
        job = _job_with_subject(ctx, gmail, png_bytes, "Call me on +44 7700 900123")

        PiiStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "pii.phone")
        assert finding.where == "email.subject"
        assert "900123" not in finding.detail  # the value itself is masked

    def test_pii_in_the_body_snippet_is_caught(self, ctx, gmail, png_bytes) -> None:
        job = _job_with_subject(ctx, gmail, png_bytes, "Holiday", snippet="mail alice@example.com")

        PiiStage().run(job, ctx)

        assert any(f.kind == "pii.email" and f.where == "email.body" for f in job.findings)

    def test_clean_subject_produces_no_pii_findings(self, ctx, gmail, png_bytes) -> None:
        job = _job_with_subject(ctx, gmail, png_bytes, "Sunset over the lake")

        PiiStage().run(job, ctx)

        assert not any(f.kind.startswith("pii.") and f.severity is Severity.warn
                       and "confidence" in f.detail for f in job.findings)


class TestPolicy:
    def test_warn_policy_surfaces_but_does_not_block(self, ctx, gmail, png_bytes) -> None:
        job = _job_with_subject(ctx, gmail, png_bytes, "Card 4111 1111 1111 1111")

        PiiStage().run(job, ctx)  # must not raise

        finding = next(f for f in job.findings if f.kind == "pii.payment_card")
        assert finding.severity is Severity.warn
        assert finding.action_taken == "flagged for review"

    def test_block_policy_quarantines_high_confidence_hits(
        self, ctx, gmail, png_bytes, monkeypatch
    ) -> None:
        _reload_settings(ctx, monkeypatch, YTSHORT_PII_POLICY="block")
        job = _job_with_subject(ctx, gmail, png_bytes, "Card 4111 1111 1111 1111")

        with pytest.raises(HaltPipeline, match="PII policy"):
            PiiStage().run(job, ctx)

        assert any(f.kind == "pii.payment_card" for f in job.blocking_findings)

    def test_block_policy_never_blocks_on_a_medium_confidence_guess(
        self, ctx, gmail, png_bytes, monkeypatch
    ) -> None:
        # A bare number that merely looks phone-shaped must not quarantine a job.
        _reload_settings(ctx, monkeypatch, YTSHORT_PII_POLICY="block")
        job = _job_with_subject(ctx, gmail, png_bytes, "Order 07700900123 shipped")

        PiiStage().run(job, ctx)  # must not raise

        finding = next(f for f in job.findings if f.kind == "pii.phone")
        assert finding.severity is Severity.warn


class TestHonestReporting:
    def test_missing_ocr_is_reported_rather_than_passing_silently(
        self, ctx, gmail, png_bytes, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "ytshort.stages.pii._ocr", lambda path: ("", "pytesseract is not installed")
        )
        job = _job_with_subject(ctx, gmail, png_bytes, "Holiday")

        PiiStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "pii.not_screened")
        assert finding.severity is Severity.warn
        assert "not installed" in finding.detail

    def test_text_found_by_ocr_is_screened(self, ctx, gmail, png_bytes, monkeypatch) -> None:
        monkeypatch.setattr(
            "ytshort.stages.pii._ocr", lambda path: ("contact bob@example.com", None)
        )
        job = _job_with_subject(ctx, gmail, png_bytes, "Holiday")

        PiiStage().run(job, ctx)

        assert any(f.kind == "pii.email" and f.where == "pic.png" for f in job.findings)

    def test_ocr_failure_does_not_fail_the_job(self, ctx, gmail, png_bytes, monkeypatch) -> None:
        monkeypatch.setattr(
            "ytshort.stages.pii._ocr", lambda path: ("", "OCR failed (broken)")
        )
        job = _job_with_subject(ctx, gmail, png_bytes, "Holiday")

        PiiStage().run(job, ctx)

        assert any(f.kind == "pii.not_screened" for f in job.findings)
