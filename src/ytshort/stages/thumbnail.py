"""Render the thumbnail: the source image as-is, under the email subject.

Two sizes come out of one layout:

* ``1080x1920`` -- used as the bumper spliced onto both ends of the Short.
* ``1280x720``  -- uploaded via ``thumbnails.set``.

"As is" is a PRD requirement and is taken literally: the source image is
*contain*-fitted, never cropped, so nothing in it is cut off. The letterbox is
filled with a blurred, darkened copy of the same image, which keeps the frame
from looking broken without altering the picture itself.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ytshort.contracts.models import Finding, Job, Severity
from ytshort.observability.logging import get_logger
from ytshort.pipeline.signals import HaltPipeline
from ytshort.pipeline.stage import BaseStage, PipelineContext

log = get_logger(__name__)

TALL_SIZE = (1080, 1920)
WIDE_SIZE = (1280, 720)
MAX_TITLE_CHARS = 90

# Candidate fonts, best first. Bundled fonts win so output is identical across
# machines; the Windows faces are the practical fallback; Pillow's built-in
# Aileron is the last resort so rendering never hard-fails on a bare box.
_FONT_CANDIDATES = (
    "assets/fonts/title.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitise_title(raw: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Make an untrusted email subject safe and sane to render.

    Strips control characters and bidi overrides (which can visually reorder a
    title into something it does not say), collapses whitespace, and truncates on
    a word boundary.
    """
    text = unicodedata.normalize("NFC", raw or "")
    text = _CONTROL_CHARS.sub(" ", text)
    # Explicit bidi controls -- a subject can otherwise render right-to-left.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return "Untitled Short"
    if len(text) <= limit:
        return text

    clipped = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return f"{clipped.rstrip()}…"


def _load_font(size: int, project_root: Path) -> ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_absolute():
            path = project_root / path
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    # Pillow >= 10.1 returns a scalable default here rather than a fixed bitmap.
    return ImageFont.load_default(size)


def _wrap(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text(
    text: str,
    draw: ImageDraw.ImageDraw,
    project_root: Path,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Shrink the font until the wrapped title fits the allotted box."""
    size = start_size
    while size >= min_size:
        font = _load_font(size, project_root)
        lines = _wrap(text, font, max_width, draw)
        line_height = int(size * 1.2)
        if len(lines) * line_height <= max_height:
            return font, lines, line_height
        size -= 4

    font = _load_font(min_size, project_root)
    lines = _wrap(text, font, max_width, draw)
    line_height = int(min_size * 1.2)
    # Hard cap the line count so a pathological subject cannot overflow the frame.
    max_lines = max(1, max_height // line_height)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return font, lines, line_height


def _backdrop(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Blurred, darkened cover-crop of the source, used to fill the letterbox."""
    target_w, target_h = size
    scale = max(target_w / source.width, target_h / source.height)
    scaled = source.resize(
        (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
        Image.LANCZOS,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    cropped = scaled.crop((left, top, left + target_w, top + target_h))
    blurred = cropped.filter(ImageFilter.GaussianBlur(radius=max(target_w, target_h) // 45))
    return Image.blend(blurred, Image.new("RGB", size, (12, 12, 16)), alpha=0.55)


def render_thumbnail(
    source_path: Path,
    title: str,
    output_path: Path,
    size: tuple[int, int],
    project_root: Path,
) -> Path:
    canvas_w, canvas_h = size
    tall = canvas_h > canvas_w

    with Image.open(source_path) as raw:
        source = raw.convert("RGB")

        canvas = _backdrop(source, size)
        draw = ImageDraw.Draw(canvas)

        margin = int(canvas_w * 0.06)
        content_width = canvas_w - 2 * margin
        gap = int(canvas_h * 0.035)

        font, lines, line_height = _fit_text(
            sanitise_title(title),
            draw,
            project_root,
            max_width=content_width,
            max_height=int(canvas_h * (0.22 if tall else 0.28)),
            start_size=int(canvas_h * (0.052 if tall else 0.085)),
            min_size=int(canvas_h * (0.024 if tall else 0.04)),
        )
        text_height = len(lines) * line_height

        # Fit the picture into whatever the title left behind...
        available_height = canvas_h - 2 * margin - text_height - gap
        fit = min(content_width / source.width, available_height / source.height)
        drawn = source.resize(
            (max(1, int(source.width * fit)), max(1, int(source.height * fit))),
            Image.LANCZOS,
        )

        # ...then centre title and picture *together*. Centring the picture in
        # the leftover space instead leaves a landscape photo stranded low in a
        # 9:16 frame with a dead band above it.
        block_height = text_height + gap + drawn.height
        block_top = max(margin, (canvas_h - block_height) // 2)

        canvas.paste(
            drawn,
            ((canvas_w - drawn.width) // 2, block_top + text_height + gap),
        )

        y = block_top
        shadow_offset = max(2, canvas_h // 500)
        for line in lines:
            width = draw.textlength(line, font=font)
            x = (canvas_w - width) / 2
            draw.text(
                (x + shadow_offset, y + shadow_offset),
                line,
                font=font,
                fill=(0, 0, 0),
            )
            draw.text((x, y), line, font=font, fill=(255, 255, 255))
            y += line_height

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="JPEG", quality=92, optimize=True)

    return output_path


class ThumbnailStage(BaseStage):
    name = "thumbnail"
    success_state = None

    def run(self, job: Job, ctx: PipelineContext) -> None:
        source_name = job.media.primary_image
        if source_name is None:
            # Falling back to a video frame is a v2 feature; be explicit rather
            # than silently producing a title card with no picture.
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="thumbnail.no_source_image",
                    severity=Severity.blocking,
                    where="email",
                    detail="no image attachment is available to build the thumbnail from",
                    action_taken="halted",
                )
            )
            raise HaltPipeline("no source image for the thumbnail")

        source = ctx.media_store.resolve(job.job_id, source_name)
        job_dir = ctx.media_store.job_dir(job.job_id)
        title = job.title or job.source.subject

        from ytshort.config import PROJECT_ROOT

        tall = render_thumbnail(
            source, title, job_dir / "thumbnail_tall.jpg", TALL_SIZE, PROJECT_ROOT
        )
        wide = render_thumbnail(
            source, title, job_dir / "thumbnail_wide.jpg", WIDE_SIZE, PROJECT_ROOT
        )

        job.media.thumbnail_tall = tall.name
        job.media.thumbnail_wide = wide.name
        log.info("thumbnails rendered", extra={"tall": tall.name, "wide": wide.name})

        # thumbnails.set rejects anything over 2 MB.
        if wide.stat().st_size > 2 * 1024 * 1024:
            job.add_finding(
                Finding(
                    stage=self.name,
                    kind="thumbnail.oversize",
                    severity=Severity.warn,
                    where=wide.name,
                    detail=(
                        f"thumbnail is {wide.stat().st_size} bytes; YouTube rejects "
                        "custom thumbnails over 2 MB"
                    ),
                    action_taken="upload may be skipped",
                )
            )
