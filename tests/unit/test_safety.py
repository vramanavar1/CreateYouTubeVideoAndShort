"""The security gate: does a file get through only if it is what it claims to be?"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from tests.conftest import make_png
from tests.fakes import FakeModerator, FakeScanner
from ytshort.contracts.models import Job, Severity, SourceEmail, make_job_id
from ytshort.pipeline.signals import HaltPipeline
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.stages.safety import SafetyStage


def _ingested(ctx, gmail, files: dict[str, bytes]) -> Job:
    gmail.add_message("m1", subject="Test", files=files)
    job = discover_jobs(ctx)[0]
    IngestStage().run(job, ctx)
    return job


def _jpeg_with_gps() -> bytes:
    """A JPEG whose EXIF carries GPS coordinates -- the holiday-snap leak case."""
    image = Image.new("RGB", (320, 240), (10, 120, 90))
    exif = image.getexif()
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (IFDRational(51, 1), IFDRational(30, 1), IFDRational(0, 1))
    gps[3] = "W"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class TestMagicBytes:
    def test_a_real_png_passes(self, ctx, gmail, png_bytes) -> None:
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        SafetyStage().run(job, ctx)

        assert job.blocking_findings == []
        assert job.media_attachments

    def test_an_executable_renamed_to_png_is_blocked(self, ctx, gmail, png_bytes) -> None:
        # A real PE header, so this is genuinely an .exe wearing a .png name.
        payload = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200 + b"PE\x00\x00"
        job = _ingested(ctx, gmail, {"pic.png": payload, "ok.png": png_bytes})

        with pytest.raises(HaltPipeline, match="safety screening"):
            SafetyStage().run(job, ctx)

        blocked = job.blocking_findings
        assert blocked
        assert blocked[0].kind in ("content.unrecognised", "content.type_mismatch")
        assert blocked[0].where == "pic.png"

    def test_extension_mismatch_is_blocked(self, ctx, gmail, png_bytes) -> None:
        # Real PNG bytes, but the email called it an .mp4.
        job = _ingested(ctx, gmail, {"clip.mp4": png_bytes})

        with pytest.raises(HaltPipeline):
            SafetyStage().run(job, ctx)

        assert any(f.kind == "content.type_mismatch" for f in job.blocking_findings)

    def test_truncated_garbage_is_blocked(self, ctx, gmail) -> None:
        job = _ingested(ctx, gmail, {"pic.png": b"not an image at all"})

        with pytest.raises(HaltPipeline):
            SafetyStage().run(job, ctx)


class TestImageGuards:
    def test_a_pixel_bomb_is_blocked(self, ctx, gmail, monkeypatch) -> None:
        job = _ingested(ctx, gmail, {"pic.png": make_png(4000, 3000)})
        monkeypatch.setattr("ytshort.stages.safety.MAX_IMAGE_PIXELS", 1_000_000)

        with pytest.raises(HaltPipeline):
            SafetyStage().run(job, ctx)

        assert any(f.kind == "image.too_many_pixels" for f in job.blocking_findings)

    def test_gps_metadata_is_stripped_and_reported(self, ctx, gmail) -> None:
        job = _ingested(ctx, gmail, {"pic.jpg": _jpeg_with_gps()})

        SafetyStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "image.metadata_stripped")
        assert finding.severity is Severity.warn
        assert "GPS" in finding.detail

        # The stored file is now the cleaned copy, and it carries no EXIF.
        cleaned = ctx.media_store.resolve(job.job_id, job.media.primary_image)
        with Image.open(cleaned) as image:
            assert not image.getexif().get_ifd(0x8825)

    def test_the_primary_image_points_at_the_cleaned_copy(self, ctx, gmail, png_bytes) -> None:
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        SafetyStage().run(job, ctx)

        assert job.media.primary_image == "pic_clean.png"
        assert ctx.media_store.resolve(job.job_id, job.media.primary_image).exists()


class TestMalwareGate:
    def test_a_detection_blocks_the_job(self, ctx, gmail, png_bytes) -> None:
        ctx.scanner = FakeScanner(clean=False, detail="Trojan:Win32/Test")
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        with pytest.raises(HaltPipeline):
            SafetyStage().run(job, ctx)

        finding = next(f for f in job.blocking_findings if f.kind == "malware.detected")
        assert "Trojan" in finding.detail

    def test_an_unavailable_scanner_warns_rather_than_passing_silently(
        self, ctx, gmail, png_bytes
    ) -> None:
        ctx.scanner = FakeScanner(skipped=True, detail="MpCmdRun.exe not found")
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        SafetyStage().run(job, ctx)

        warning = next(f for f in job.findings if f.kind == "malware.not_scanned")
        assert warning.severity is Severity.warn
        # Crucially, it is NOT recorded as clean.
        assert not any(f.kind == "malware.clean" for f in job.findings)


class TestModerationGate:
    def test_flagged_content_blocks(self, ctx, gmail, png_bytes) -> None:
        ctx.moderator = FakeModerator(
            flagged=True, categories=["graphic_violence"], detail="depicts injury"
        )
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        with pytest.raises(HaltPipeline):
            SafetyStage().run(job, ctx)

        finding = next(f for f in job.blocking_findings if f.kind == "moderation.flagged")
        assert "graphic_violence" in finding.detail

    def test_a_disabled_moderator_adds_no_noise(self, ctx, gmail, png_bytes) -> None:
        ctx.moderator = FakeModerator(name="none", skipped=True)
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        SafetyStage().run(job, ctx)

        assert not any(f.kind == "moderation.not_screened" for f in job.findings)

    def test_an_unavailable_moderator_warns(self, ctx, gmail, png_bytes) -> None:
        ctx.moderator = FakeModerator(name="claude", skipped=True, detail="API unreachable")
        job = _ingested(ctx, gmail, {"pic.png": png_bytes})

        SafetyStage().run(job, ctx)

        assert any(f.kind == "moderation.not_screened" for f in job.findings)


def test_findings_are_never_lost_between_stages(ctx, gmail, png_bytes) -> None:
    job = Job(job_id=make_job_id("m1"), source=SourceEmail(message_id="m1"))
    gmail.add_message("m1", files={"pic.png": png_bytes})
    job = _ingested(ctx, gmail, {"pic.png": png_bytes})
    before = len(job.findings)

    SafetyStage().run(job, ctx)

    assert len(job.findings) >= before
