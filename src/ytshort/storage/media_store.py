"""Per-job media directory with sanitised filenames.

Attachment filenames come from an email, which means they are attacker-supplied.
Everything written here goes through :func:`safe_filename` so a crafted name like
``../../../.ssh/authorized_keys`` cannot escape the job directory.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 64


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """Reduce an untrusted filename to a flat, printable, bounded basename."""
    # Take the basename under both separators before sanitising -- a Windows-style
    # "..\\..\\x" must not survive as a path.
    base = name.replace("\\", "/").split("/")[-1]
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    stem = _UNSAFE.sub("_", stem).strip("._") or fallback
    suffix = _UNSAFE.sub("", suffix).lower()[:8]
    stem = stem[:_MAX_STEM]
    return f"{stem}.{suffix}" if suffix else stem


class MediaStore:
    def __init__(self, media_dir: Path) -> None:
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        path = self.media_dir / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, job_id: str, relative: str) -> Path:
        """Resolve a job-relative path, refusing anything outside the job dir."""
        root = self.job_dir(job_id).resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes job directory: {relative!r}")
        return candidate

    def write_bytes(self, job_id: str, filename: str, data: bytes) -> tuple[Path, str]:
        """Store bytes under a sanitised name. Returns (absolute path, sha256)."""
        target = self.job_dir(job_id) / safe_filename(filename)
        # Collisions are possible after sanitisation (two attachments reducing to
        # the same name); suffix with a counter rather than silently overwriting.
        counter = 1
        while target.exists():
            stem = target.stem
            target = target.with_name(f"{stem}_{counter}{target.suffix}")
            counter += 1
        target.write_bytes(data)
        return target, hashlib.sha256(data).hexdigest()
