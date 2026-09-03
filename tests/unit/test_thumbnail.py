"""Thumbnail rendering and the untrusted-subject sanitiser."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import make_png
from tests.fakes import FakeArtDirector
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


class TestArtDirection:
    """Optional direction from a model, honoured by a renderer that stays in charge."""

    def _source(self, tmp_path) -> Path:
        source = tmp_path / "src.png"
        source.write_bytes(make_png(1200, 800))
        return source

    def test_direction_is_optional(self, tmp_path) -> None:
        # Existing callers pass none of it and must be unaffected.
        output = render_thumbnail(
            self._source(tmp_path), "A title", tmp_path / "o.jpg", TALL_SIZE, PROJECT_ROOT
        )

        assert output.exists()

    def test_emphasis_changes_the_pixels(self, tmp_path) -> None:
        source = self._source(tmp_path)
        plain = render_thumbnail(
            source, "GOLDEN HOUR", tmp_path / "plain.jpg", TALL_SIZE, PROJECT_ROOT
        )
        accented = render_thumbnail(
            source,
            "GOLDEN HOUR",
            tmp_path / "accent.jpg",
            TALL_SIZE,
            PROJECT_ROOT,
            emphasis="GOLDEN",
            accent_hex="#FF0000",
        )

        assert plain.read_bytes() != accented.read_bytes()

    def test_an_emphasis_that_matches_nothing_renders_as_plain(self, tmp_path) -> None:
        source = self._source(tmp_path)
        plain = render_thumbnail(
            source, "GOLDEN HOUR", tmp_path / "a.jpg", TALL_SIZE, PROJECT_ROOT
        )
        missing = render_thumbnail(
            source,
            "GOLDEN HOUR",
            tmp_path / "b.jpg",
            TALL_SIZE,
            PROJECT_ROOT,
            emphasis="OCEAN",
            accent_hex="#FF0000",
        )

        assert plain.read_bytes() == missing.read_bytes()

    def test_emphasis_ignores_trailing_punctuation(self, tmp_path) -> None:
        source = self._source(tmp_path)
        plain = render_thumbnail(source, "THAT SKY!", tmp_path / "a.jpg", TALL_SIZE, PROJECT_ROOT)
        accented = render_thumbnail(
            source,
            "THAT SKY!",
            tmp_path / "b.jpg",
            TALL_SIZE,
            PROJECT_ROOT,
            emphasis="SKY",
            accent_hex="#00FF00",
        )

        assert plain.read_bytes() != accented.read_bytes()

    def test_text_position_moves_the_block(self, tmp_path) -> None:
        source = self._source(tmp_path)
        top = render_thumbnail(
            source, "A title", tmp_path / "top.jpg", TALL_SIZE, PROJECT_ROOT, text_position="top"
        )
        bottom = render_thumbnail(
            source,
            "A title",
            tmp_path / "bottom.jpg",
            TALL_SIZE,
            PROJECT_ROOT,
            text_position="bottom",
        )

        assert top.read_bytes() != bottom.read_bytes()
        with Image.open(bottom) as rendered:
            assert rendered.size == TALL_SIZE

    def test_a_hostile_hook_is_still_sanitised(self, tmp_path) -> None:
        # A model can emit control characters and bidi overrides just as an email
        # subject can. The renderer must not care where the text came from.
        output = render_thumbnail(
            self._source(tmp_path),
            "GOLDEN\u202E\x07 HOUR",
            tmp_path / "o.jpg",
            TALL_SIZE,
            PROJECT_ROOT,
            emphasis="GOLDEN",
        )

        with Image.open(output) as rendered:
            assert rendered.size == TALL_SIZE


class TestArtDirectorFindings:
    """A plain-subject thumbnail must be distinguishable from a directed one."""

    def _job_with_image(self, ctx, gmail, png_bytes):
        gmail.add_message("m1", subject="Sunset over the lake", files={"pic.png": png_bytes})
        job = discover_jobs(ctx)[0]
        IngestStage().run(job, ctx)
        return job

    def test_a_generated_hook_is_recorded_and_used(self, ctx, gmail, png_bytes) -> None:
        ctx.art_director = FakeArtDirector()
        job = self._job_with_image(ctx, gmail, png_bytes)

        ThumbnailStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "thumbnail.hooks_generated")
        assert finding.severity is Severity.info
        assert job.thumbnail_hooks == ["GOLDEN HOUR", "THAT SKY", "UNREAL LIGHT"]
        assert job.thumbnail_text == "GOLDEN HOUR"

    def test_no_director_records_an_info_finding_and_uses_the_subject(
        self, ctx, gmail, png_bytes
    ) -> None:
        # Nothing configured is a choice, not a degraded run -- but it is still
        # recorded, so this job cannot be mistaken for a directed one.
        ctx.art_director = None
        job = self._job_with_image(ctx, gmail, png_bytes)

        ThumbnailStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "thumbnail.hooks_not_generated")
        assert finding.severity is Severity.info
        assert job.thumbnail_text == "Sunset over the lake"

    def test_a_failed_director_warns_rather_than_failing_the_job(
        self, ctx, gmail, png_bytes
    ) -> None:
        ctx.art_director = FakeArtDirector(name="foundry", skipped=True, detail="call failed")
        job = self._job_with_image(ctx, gmail, png_bytes)

        ThumbnailStage().run(job, ctx)

        finding = next(f for f in job.findings if f.kind == "thumbnail.hooks_not_generated")
        assert finding.severity is Severity.warn
        assert "call failed" in finding.detail
        assert job.media.thumbnail_wide  # rendered anyway

    def test_a_reviewers_choice_survives_a_re_run(self, ctx, gmail, png_bytes) -> None:
        # The thumbnail stage can run again (a retry, or a cleared record). It must
        # not overwrite text the reviewer already picked.
        ctx.art_director = FakeArtDirector()
        job = self._job_with_image(ctx, gmail, png_bytes)
        job.thumbnail_text = "MY OWN WORDS"

        ThumbnailStage().run(job, ctx)

        assert job.thumbnail_text == "MY OWN WORDS"
