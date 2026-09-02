"""Thin subprocess wrapper around ffmpeg/ffprobe.

Shelling out rather than binding a Python library is deliberate: it keeps GPL/
LGPL-licensed code out of this process, matches how ffmpeg is actually
documented, and means the exact command can be logged and re-run by hand when a
render misbehaves.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

_PROBE_TIMEOUT = 60
_RENDER_TIMEOUT = 900


class FFmpegNotAvailable(RuntimeError):
    """ffmpeg or ffprobe could not be located."""


class FFmpegError(RuntimeError):
    """A render or probe command failed."""


@dataclass
class MediaProbe:
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    video_stream_count: int
    audio_stream_count: int
    format_name: str

    @property
    def is_vertical(self) -> bool:
        return self.height >= self.width


def _resolve(configured: str, default_name: str) -> str | None:
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
        # A configured value that is just a command name is still worth trying.
        return shutil.which(configured)
    return shutil.which(default_name)


class FFmpeg:
    def __init__(self, ffmpeg_path: str = "", ffprobe_path: str = "") -> None:
        self.ffmpeg = _resolve(ffmpeg_path, "ffmpeg")
        self.ffprobe = _resolve(ffprobe_path, "ffprobe")

    @classmethod
    def from_settings(cls, settings) -> FFmpeg:
        return cls(settings.ffmpeg_path, settings.ffprobe_path)

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None

    def require(self) -> None:
        if not self.available:
            missing = [
                name
                for name, value in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe))
                if value is None
            ]
            raise FFmpegNotAvailable(
                f"{' and '.join(missing)} not found. Install ffmpeg (winget install "
                "Gyan.FFmpeg) or set YTSHORT_FFMPEG_PATH / YTSHORT_FFPROBE_PATH."
            )

    # -- probing -----------------------------------------------------------
    def probe(self, path: Path) -> MediaProbe:
        self.require()
        assert self.ffprobe is not None  # narrowed by require()

        completed = subprocess.run(  # noqa: S603 - resolved binary, no shell
            [
                self.ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            raise FFmpegError(f"ffprobe failed on {path.name}: {completed.stderr.strip()}")

        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FFmpegError(f"ffprobe returned unparseable output for {path.name}") from exc

        streams = data.get("streams", []) or []
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        fmt = data.get("format", {}) or {}

        # Duration can live on the format or on the stream depending on the
        # container; prefer the format and fall back.
        duration = fmt.get("duration") or (video[0].get("duration") if video else None) or 0
        first_video = video[0] if video else {}

        return MediaProbe(
            duration_seconds=float(duration),
            width=int(first_video.get("width", 0) or 0),
            height=int(first_video.get("height", 0) or 0),
            has_audio=bool(audio),
            video_stream_count=len(video),
            audio_stream_count=len(audio),
            format_name=fmt.get("format_name", ""),
        )

    # -- rendering ---------------------------------------------------------
    def run(self, args: list[str], *, description: str = "ffmpeg") -> None:
        self.require()
        assert self.ffmpeg is not None

        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
        log.debug("running ffmpeg", extra={"description": description})
        completed = subprocess.run(  # noqa: S603 - resolved binary, no shell
            command,
            capture_output=True,
            text=True,
            timeout=_RENDER_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            # The full command is worth having in the log -- these failures are
            # almost always a filter-graph problem you want to re-run by hand.
            log.error(
                "ffmpeg failed",
                extra={"description": description, "command": " ".join(command)},
            )
            raise FFmpegError(f"{description} failed: {completed.stderr.strip()[:2000]}")
