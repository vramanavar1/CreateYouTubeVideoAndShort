"""Background-track selection from a local folder of licensed audio.

This is the replacement for the PRD's original "use the audio from this YouTube
video" instruction. Downloading a copyrighted track from YouTube breaks its Terms
of Service, so the pipeline never fetches audio -- you supply files you have the
right to use (the YouTube Audio Library is a good free source) and this module
picks one.

Selection is deterministic per job: the same job always gets the same track, so a
re-render after an approval produces the same output, while different jobs still
rotate through the folder.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"})


class NoAudioAvailable(RuntimeError):
    """The audio folder is missing or holds no usable track."""


class AudioSource:
    def __init__(self, audio_dir: Path) -> None:
        self.audio_dir = audio_dir

    def tracks(self) -> list[Path]:
        if not self.audio_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.audio_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        )

    def select(self, seed: str) -> Path:
        """Pick a track deterministically from ``seed`` (the job id)."""
        available = self.tracks()
        if not available:
            raise NoAudioAvailable(
                f"No licensed audio track found in {self.audio_dir}. Drop an .mp3 you "
                "have the rights to use (YouTube Studio > Audio library is a free "
                "source) and record it in AUDIO_LICENSES.md."
            )

        # Hash the seed rather than slicing it: any seed works, and seeds sharing
        # a prefix (or made mostly of leading zeros) still spread across tracks.
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        chosen = available[int(digest[:8], 16) % len(available)]
        log.info("background track selected", extra={"track": chosen.name})
        return chosen
