"""One JSON document per job, written atomically.

Atomicity matters more than it looks: the runner persists after *every* stage,
and a job record truncated by an interrupted write would strand media that has
already been downloaded or, worse, a video that has already been uploaded. Write
to a temp file in the same directory, then ``os.replace`` -- atomic on Windows
and POSIX alike.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from ytshort.contracts.models import Job, JobState, utcnow


class JobStore:
    def __init__(self, jobs_dir: Path) -> None:
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def exists(self, job_id: str) -> bool:
        return self.path_for(job_id).exists()

    def save(self, job: Job) -> Path:
        job.updated_at = utcnow()
        target = self.path_for(job.job_id)
        payload = job.model_dump_json(indent=2)

        # NamedTemporaryFile in the *same* directory, so os.replace stays on one
        # filesystem and therefore stays atomic.
        fd, tmp_name = tempfile.mkstemp(dir=self.jobs_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return target

    def load(self, job_id: str) -> Job | None:
        path = self.path_for(job_id)
        if not path.exists():
            return None
        return Job.model_validate_json(path.read_text(encoding="utf-8"))

    def iter_jobs(self) -> Iterator[Job]:
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                yield Job.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt record must not kill a listing
                continue

    def list_jobs(self, state: JobState | None = None) -> list[Job]:
        jobs = [j for j in self.iter_jobs() if state is None or j.state is state]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def known_message_ids(self) -> set[str]:
        """Gmail message ids that already have a job -- the ingest dedupe set."""
        return {job.source.message_id for job in self.iter_jobs()}
