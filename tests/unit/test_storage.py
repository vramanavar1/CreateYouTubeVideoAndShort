"""Job store, media store, and the daily counter."""

from __future__ import annotations

import pytest

from ytshort.contracts.models import Job, JobState, SourceEmail, make_job_id
from ytshort.storage.counters import DailyCounter
from ytshort.storage.job_store import JobStore
from ytshort.storage.media_store import MediaStore, safe_filename


def _job(message_id: str = "m1") -> Job:
    return Job(job_id=make_job_id(message_id), source=SourceEmail(message_id=message_id))


class TestJobStore:
    def test_round_trip_preserves_state(self, settings) -> None:
        store = JobStore(settings.jobs_dir)
        job = _job()
        job.state = JobState.composed
        job.title = "Café ☕ — naïve"  # non-ASCII must survive the JSON round trip
        store.save(job)

        loaded = store.load(job.job_id)
        assert loaded is not None
        assert loaded.state is JobState.composed
        assert loaded.title == "Café ☕ — naïve"

    def test_save_is_atomic_and_leaves_no_temp_files(self, settings) -> None:
        store = JobStore(settings.jobs_dir)
        job = _job()
        store.save(job)
        store.save(job)

        assert not list(settings.jobs_dir.glob("*.tmp"))
        assert len(list(settings.jobs_dir.glob("*.json"))) == 1

    def test_known_message_ids_backs_ingest_dedupe(self, settings) -> None:
        store = JobStore(settings.jobs_dir)
        store.save(_job("m1"))
        store.save(_job("m2"))

        assert store.known_message_ids() == {"m1", "m2"}

    def test_list_filters_by_state(self, settings) -> None:
        store = JobStore(settings.jobs_dir)
        parked = _job("m1")
        parked.state = JobState.awaiting_review
        store.save(parked)
        store.save(_job("m2"))

        assert [j.job_id for j in store.list_jobs(JobState.awaiting_review)] == [parked.job_id]

    def test_corrupt_record_does_not_break_a_listing(self, settings) -> None:
        store = JobStore(settings.jobs_dir)
        store.save(_job("m1"))
        (settings.jobs_dir / "broken.json").write_text("{not json", encoding="utf-8")

        assert len(list(store.iter_jobs())) == 1

    def test_job_id_is_stable_per_message(self) -> None:
        assert make_job_id("abc") == make_job_id("abc")
        assert make_job_id("abc") != make_job_id("abd")


class TestSafeFilename:
    @pytest.mark.parametrize(
        "raw",
        [
            "../../../etc/passwd",
            r"..\..\..\Windows\System32\drivers\etc\hosts",
            "/absolute/path.png",
            "C:/Windows/evil.png",
        ],
    )
    def test_traversal_is_flattened(self, raw: str) -> None:
        result = safe_filename(raw)
        assert "/" not in result
        assert "\\" not in result
        assert not result.startswith("..")

    def test_keeps_a_usable_name(self) -> None:
        assert safe_filename("holiday photo.JPG") == "holiday_photo.jpg"

    def test_empty_name_falls_back(self) -> None:
        assert safe_filename("...") == "attachment"

    def test_long_name_is_bounded(self) -> None:
        assert len(safe_filename("a" * 500 + ".png")) <= 73


class TestMediaStore:
    def test_write_bytes_returns_digest(self, settings) -> None:
        store = MediaStore(settings.media_dir)
        path, digest = store.write_bytes("job1", "pic.png", b"hello")

        assert path.read_bytes() == b"hello"
        assert len(digest) == 64

    def test_collisions_do_not_overwrite(self, settings) -> None:
        store = MediaStore(settings.media_dir)
        first, _ = store.write_bytes("job1", "pic.png", b"one")
        second, _ = store.write_bytes("job1", "pic.png", b"two")

        assert first != second
        assert first.read_bytes() == b"one"
        assert second.read_bytes() == b"two"

    def test_resolve_refuses_to_escape_the_job_directory(self, settings) -> None:
        store = MediaStore(settings.media_dir)
        with pytest.raises(ValueError, match="escapes job directory"):
            store.resolve("job1", "../../secrets.txt")


class TestDailyCounter:
    def test_counts_and_caps(self, settings) -> None:
        counter = DailyCounter(settings.counters_dir)
        assert counter.remaining(10) == 10

        for _ in range(4):
            counter.increment()

        assert counter.count() == 4
        assert counter.remaining(10) == 6

    def test_remaining_never_goes_negative(self, settings) -> None:
        counter = DailyCounter(settings.counters_dir)
        counter.increment(25)
        assert counter.remaining(10) == 0

    def test_survives_a_corrupt_file(self, settings) -> None:
        counter = DailyCounter(settings.counters_dir)
        counter.path.write_text("garbage", encoding="utf-8")
        assert counter.count() == 0
