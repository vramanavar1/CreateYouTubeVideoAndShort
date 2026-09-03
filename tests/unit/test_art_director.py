"""The art director, and the sanitising that stands between it and a published image.

Model output is untrusted text on its way into an image that gets uploaded to
YouTube. These tests are mostly about that, not about hook quality.
"""

from __future__ import annotations

import pytest

from ytshort.integrations.art_director import (
    FoundryArtDirector,
    NoopArtDirector,
    ThumbnailDirection,
    build_art_director,
)


class TestBuildArtDirector:
    def test_default_is_a_noop(self) -> None:
        assert isinstance(build_art_director("none"), NoopArtDirector)
        assert isinstance(build_art_director(""), NoopArtDirector)

    def test_foundry_without_an_endpoint_degrades(self) -> None:
        # A misconfigured director must not fail jobs -- the thumbnail still
        # renders, just with the subject.
        assert isinstance(build_art_director("foundry", endpoint=""), NoopArtDirector)

    def test_foundry_with_an_endpoint_is_built(self) -> None:
        director = build_art_director("foundry", endpoint="https://x.openai.azure.com/openai/v1/")
        assert isinstance(director, FoundryArtDirector)

    def test_unknown_provider_degrades(self) -> None:
        assert isinstance(build_art_director("hal9000"), NoopArtDirector)


class TestNoop:
    def test_returns_the_subject_and_says_it_skipped(self, tmp_path) -> None:
        result = NoopArtDirector().direct(tmp_path / "a.jpg", "Sunset over the lake", "")

        assert result.hooks == ["Sunset over the lake"]
        assert result.skipped is True


class TestSanitising:
    """`sanitised()` is the boundary. Everything past it reaches a rendered image."""

    def test_a_malformed_accent_falls_back_to_white(self) -> None:
        for bad in ("red", "#GGGGGG", "#FFF", "", "#FFFFFF; drop table", "FFFFFF"):
            result = ThumbnailDirection(hooks=["a"], accent_hex=bad).sanitised("s")
            assert result.accent_hex == "#FFFFFF", bad

    def test_a_valid_accent_is_kept(self) -> None:
        assert ThumbnailDirection(hooks=["a"], accent_hex="#ffd400").sanitised("s").accent_hex == (
            "#ffd400"
        )

    def test_emphasis_absent_from_the_hooks_is_dropped(self) -> None:
        # Otherwise it colours nothing, or worse, a substring of another word.
        result = ThumbnailDirection(hooks=["GOLDEN HOUR"], emphasis="OCEAN").sanitised("s")

        assert result.emphasis == ""

    def test_emphasis_present_in_the_hooks_is_kept(self) -> None:
        result = ThumbnailDirection(hooks=["GOLDEN HOUR"], emphasis="golden").sanitised("s")

        assert result.emphasis == "golden"

    def test_an_unknown_text_position_falls_back_to_top(self) -> None:
        result = ThumbnailDirection(hooks=["a"], text_position="diagonal").sanitised("s")

        assert result.text_position == "top"

    def test_overlong_hooks_are_truncated(self) -> None:
        result = ThumbnailDirection(hooks=["word " * 100]).sanitised("s")

        assert len(result.hooks[0]) <= 48

    def test_empty_hooks_fall_back_to_the_subject(self) -> None:
        result = ThumbnailDirection(hooks=["", "   "]).sanitised("Sunset over the lake")

        assert result.hooks == ["Sunset over the lake"]

    def test_a_long_rationale_is_bounded(self) -> None:
        result = ThumbnailDirection(hooks=["a"], rationale="x" * 900).sanitised("s")

        assert len(result.rationale) <= 200


class TestFoundryDegradesRatherThanRaises:
    """A thumbnail hook is never worth failing a job over."""

    @pytest.fixture
    def director(self) -> FoundryArtDirector:
        return FoundryArtDirector("https://x.openai.azure.com/openai/v1/", "gpt-4o-mini")

    def test_an_unsupported_file_type_is_skipped(self, director, tmp_path) -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00")

        result = director.direct(path, "Subject", "")

        assert result.skipped is True
        assert result.hooks == ["Subject"]

    def test_an_unreadable_image_is_skipped(self, director, tmp_path) -> None:
        path = tmp_path / "broken.png"
        path.write_bytes(b"not actually a png")

        result = director.direct(path, "Subject", "")

        assert result.skipped is True
        assert "Subject" in result.hooks

    def test_a_missing_sdk_or_failed_call_never_raises(self, director, tmp_path, png_bytes) -> None:
        # openai is an optional extra; with or without it installed this must
        # return a skipped result rather than propagate.
        path = tmp_path / "pic.png"
        path.write_bytes(png_bytes)

        result = director.direct(path, "Subject", "body")

        assert result.skipped is True
        assert result.hooks == ["Subject"]
        assert result.detail
