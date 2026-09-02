"""Advisory per-job lock, so two runners never process one job at once.

In the Azure deployment there are two things that can start a run: the hourly
scheduled Job, and a run triggered by someone clicking Approve. Without a lock
both could execute the ``publish`` stage for the same job concurrently, and the
``video_id`` guard would not save us -- each process reads the job record before
the other writes it, so both see "not yet published" and both upload.

The lock is a file created with ``O_CREAT | O_EXCL``, which is atomic on Windows,
POSIX, and SMB alike. It carries the owner and a timestamp so a lock orphaned by
a crashed container can be aged out rather than wedging the job forever.

Failing to acquire is **not an error**. The other runner has the job; this one
skips it and moves on. That is why acquisition returns a bool rather than raising.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

#: A run that has held the lock longer than this is presumed dead. Generous,
#: because a long video render legitimately takes minutes.
DEFAULT_STALE_AFTER_SECONDS = 45 * 60


class JobLock:
    def __init__(
        self,
        locks_dir: Path,
        job_id: str,
        *,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self.locks_dir = Path(locks_dir)
        self.job_id = job_id
        self.stale_after_seconds = stale_after_seconds
        self.path = self.locks_dir / f"{job_id}.lock"
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def _owner(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}"

    def _write_lock(self, fd: int) -> None:
        payload = json.dumps({"owner": self._owner(), "acquired_at": time.time()})
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def _is_stale(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            acquired = float(data.get("acquired_at", 0))
        except (OSError, ValueError, TypeError):
            # An unreadable lock file is itself evidence of a crashed writer;
            # fall back to the file's mtime.
            try:
                acquired = self.path.stat().st_mtime
            except OSError:
                return False
        return (time.time() - acquired) > self.stale_after_seconds

    def acquire(self) -> bool:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._is_stale():
                return False
            log.warning(
                "breaking a stale job lock", extra={"job": self.job_id, "lock": self.path.name}
            )
            # Best-effort break, then one retry. If someone else won the race in
            # between, we simply do not get the lock this cycle.
            try:
                self.path.unlink()
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (OSError, FileExistsError):
                return False
        except OSError as exc:
            log.warning("could not create job lock", extra={"error": str(exc)})
            return False

        self._write_lock(fd)
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - unlink of our own file
            log.warning("could not release job lock", extra={"error": str(exc)})
        finally:
            self._held = False


@contextmanager
def job_lock(
    locks_dir: Path, job_id: str, *, stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
) -> Iterator[bool]:
    """Acquire for the duration of the block. Yields whether it was acquired."""
    lock = JobLock(locks_dir, job_id, stale_after_seconds=stale_after_seconds)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        lock.release()
