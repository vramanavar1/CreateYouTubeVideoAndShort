"""Thumbnail rendering and the untrusted-subject sanitiser."""

from __future__ import annotations

import pytest
from PIL import Image

from tests.conftest import make_png
from ytshort.config import PROJECT_ROOT
from ytshort.contracts.models import Severity
from ytshort.pipeline.signals import HaltPipeline
from ytshort.stages.ingest import IngestStage, discover_jobs
from ytshort.stages.safety import SafetyStage
from ytshort.stages.thumbnail import (
    TALL_SIZE,
    WIDE_SIZE,
    ThumbnailStage,
    render_thumbnail,
    sanitise_title,
)


class TestSanitiseTitle:
    def test_collapses_whitespace(self) -> None:
        assert sanitise_title("  Hello   there \n world ") == "Hello there world"

    def test_strips_control_characters(self) -> None:
        assert "\x00" not in sanitise_title("Bad\x00title\x07here")

    def test_strips_bidi_overrides(self) -> None:
        # A right-to-left override can make a title render as something else.
        cleaned = sanitise_title("photo‮gnp.exe")
        assert "‮" not in cleaned

    def test_empty_subject_gets_a_fallback(self) -> None:
        assert sanitise_title("") == "Untitled Short"
        assert sanitise_title("   ") == "Untitled Short"

    def test_truncates_on_a_word_boundary(self) -> None:
        result = sanitise_title("word " * 60, limit=30)
        assert len(result) <= 31  # the ellipsis
        assert result.endswith("…")

    def test_keeps_unicode_intact(self) -> None:
        assert sanitise_title("Café ☕ naïve") == "Café ☕ naïve"


class TestRender:
    def test_produces_the_exact_target_sizes(self, tmp_path) -> None:
        source = tmp_path / "src.png"
        source.write_bytes(make_png(800, 600))

        tall = render_thumbnail(source, "A title", tmp_path / "t.jpg", TALL_SIZE, PROJECT_ROOT)
        wide = render_thumbnail(source, "A title", tmp_path / "w.jpg", WIDE_SIZE, PROJECT_ROOT)

        with Image.open(tall) as image:
            assert image.size == TALL_SIZE
        with Image.open(wide) as image:
            assert image.size == WIDE_SIZE

    @pytest.mark.parametrize(
        "dimensions",
        [(1600, 400), (400, 1600), (100, 100), (1, 1)],
        ids=["ultrawide", "ultratall", "tiny-square", "one-pixel"],
    )
    def test_handles_any_source_aspect_ratio(self, tmp_path, dimensions) -> None:
        source = tmp_path / "src.png"
        source.write_bytes(make_png(*dimensions))

        output = render_thumbnail(source, "Title", tmp_path / "o.jpg", TALL_SIZE, PROJECT_ROOT)

        with Image.open(output) as image:
            assert image.size == TALL_SIZE

    def test_a_pathological_subject_still_renders(self, tmp_path) -> None:
        source = tmp_path / "src.png"
        source.write_bytes(make_png(800, 600))
        subject = "Supercalifragilistic" * 30  # one unbreakable 600-char word

        output = render_thumbnail(source, subject, tmp_path / "o.jpg", TALL_SIZE, PROJECT_ROOT)

        with Image.open(output) as image:
            assert image.size == TALL_SIZE


class TestThumbnailStage:
    def test_renders_both_sizes_onto_the_job(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", subject="Sunset", files={"pic.png": png_bytes})
        job = discover_jobs(ctx)[0]
        IngestStage().run(job, ctx)
        SafetyStage().run(job, ctx)

        ThumbnailStage().run(job, ctx)

        assert job.media.thumbnail_tall == "thumbnail_tall.jpg"
        assert job.media.thumbnail_wide == "thumbnail_wide.jpg"
        assert ctx.media_store.resolve(job.job_id, job.media.thumbnail_tall).exists()

    def test_halts_when_there_is_no_source_image(self, ctx, gmail, png_bytes) -> None:
        gmail.add_message("m1", files={"pic.png": png_bytes})
        job = discover_jobs(ctx)[0]
        IngestStage().run(job, ctx)
        job.media.primary_image = None

        with pytest.raises(HaltPipeline, match="no source image"):
            ThumbnailStage().run(job, ctx)

        assert any(f.severity is Severity.blocking for f in job.findings)
