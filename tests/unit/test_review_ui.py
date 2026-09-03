"""The review UI: CSRF, the approve/reject flow, and media-path safety."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_png
from ytshort.contracts.models import (
    Job,
    JobState,
    SourceEmail,
    StageRecord,
    StageStatus,
    make_job_id,
    utcnow,
)
from ytshort.pipeline.stage import PipelineContext
from ytshort.web.app import CSRF_COOKIE, create_app


@pytest.fixture
def client(settings, ctx) -> TestClient:
    app = create_app(settings)
    # Point the app at the same fake-backed context the tests use, so approving
    # exercises the real pipeline without touching Google.
    app.state.context_factory = lambda: ctx
    return TestClient(app)


def _parked_job(ctx, *, subject: str = "Sunset over the lake") -> Job:
    job = Job(
        job_id=make_job_id("m1"),
        state=JobState.awaiting_review,
        source=SourceEmail(message_id="m1", subject=subject, sender="a@example.com"),
        title=subject,
    )
    # A genuinely parked job has already completed everything up to the gate;
    # without these records a resume would re-run ingest against a fake inbox
    # that no longer holds the message.
    for name in ("ingest", "safety", "pii", "thumbnail", "compose"):
        job.stages[name] = StageRecord(
            name=name, status=StageStatus.completed, started_at=utcnow(), completed_at=utcnow()
        )
    job.media.composed_video = "short.mp4"
    job.media.thumbnail_tall = "thumbnail_tall.jpg"
    job.media.thumbnail_wide = "thumbnail_wide.jpg"
    job.media.duration_seconds = 9.0
    job_dir = ctx.media_store.job_dir(job.job_id)
    (job_dir / "short.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (job_dir / "thumbnail_tall.jpg").write_bytes(b"\xff\xd8\xff")
    (job_dir / "thumbnail_wide.jpg").write_bytes(b"\xff\xd8\xff")
    ctx.job_store.save(job)
    return job


def _csrf(client: TestClient) -> str:
    client.get("/reviews")
    return client.cookies[CSRF_COOKIE]


class TestQueue:
    def test_empty_queue_renders(self, client) -> None:
        response = client.get("/reviews")
        assert response.status_code == 200
        assert "Nothing waiting" in response.text

    def test_parked_job_appears(self, client, ctx) -> None:
        _parked_job(ctx)
        response = client.get("/reviews")
        assert "Sunset over the lake" in response.text

    def test_root_redirects_to_the_queue(self, client) -> None:
        assert client.get("/", follow_redirects=False).status_code == 307

    def test_the_api_docs_surface_is_disabled(self, client) -> None:
        # No authentication on this app, so no schema browser either.
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


class TestDetail:
    def test_shows_findings_and_the_decision_form(self, client, ctx) -> None:
        from ytshort.contracts.models import Finding, Severity

        job = _parked_job(ctx)
        job.add_finding(
            Finding(
                stage="pii",
                kind="pii.phone",
                severity=Severity.warn,
                where="email.subject",
                detail="possible phone (medium confidence): *****123",
            )
        )
        ctx.job_store.save(job)

        response = client.get(f"/reviews/{job.job_id}")

        assert "pii.phone" in response.text
        assert "Approve" in response.text

    def test_a_hostile_subject_is_escaped_not_executed(self, client, ctx) -> None:
        job = _parked_job(ctx, subject="<script>alert(1)</script>")
        response = client.get(f"/reviews/{job.job_id}")

        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_unknown_job_is_404(self, client) -> None:
        assert client.get("/reviews/nope").status_code == 404

    def test_a_decided_job_shows_no_decision_form(self, client, ctx) -> None:
        job = _parked_job(ctx)
        job.state = JobState.done
        ctx.job_store.save(job)

        response = client.get(f"/reviews/{job.job_id}")
        assert "Approve &amp; publish" not in response.text


class TestCsrf:
    def test_approve_without_a_token_is_refused(self, client, ctx, youtube) -> None:
        job = _parked_job(ctx)

        response = client.post(f"/reviews/{job.job_id}/approve", data={})

        assert response.status_code == 403
        assert youtube.uploads == []

    def test_approve_with_a_forged_token_is_refused(self, client, ctx, youtube) -> None:
        job = _parked_job(ctx)
        _csrf(client)

        response = client.post(
            f"/reviews/{job.job_id}/approve", data={"csrf_token": "forged"}
        )

        assert response.status_code == 403
        assert youtube.uploads == []

    def test_reject_is_protected_too(self, client, ctx) -> None:
        job = _parked_job(ctx)
        assert client.post(f"/reviews/{job.job_id}/reject", data={}).status_code == 403


class TestDecisions:
    def test_approving_publishes_and_distributes(self, client, ctx, youtube) -> None:
        job = _parked_job(ctx)
        token = _csrf(client)

        response = client.post(
            f"/reviews/{job.job_id}/approve",
            data={"csrf_token": token, "title": "Edited title", "tags": "travel, sunset"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert len(youtube.uploads) == 1
        assert youtube.uploads[0]["title"] == "Edited title"
        assert "travel" in youtube.uploads[0]["tags"]

        final = ctx.job_store.load(job.job_id)
        assert final is not None
        assert final.state is JobState.done
        assert final.publication is not None
        assert final.publication.short_url.startswith("https://youtu.be/")

    def test_rejecting_records_the_reason_and_publishes_nothing(
        self, client, ctx, youtube
    ) -> None:
        job = _parked_job(ctx)
        token = _csrf(client)

        client.post(
            f"/reviews/{job.job_id}/reject",
            data={"csrf_token": token, "reason": "wrong photo"},
            follow_redirects=False,
        )

        final = ctx.job_store.load(job.job_id)
        assert final is not None
        assert final.state is JobState.rejected
        assert final.review is not None
        assert final.review.reason == "wrong photo"
        assert youtube.uploads == []

    def test_deciding_twice_is_refused(self, client, ctx, youtube) -> None:
        job = _parked_job(ctx)
        token = _csrf(client)

        client.post(f"/reviews/{job.job_id}/approve", data={"csrf_token": token},
                    follow_redirects=False)
        second = client.post(f"/reviews/{job.job_id}/approve", data={"csrf_token": token},
                             follow_redirects=False)

        assert second.status_code == 409
        assert len(youtube.uploads) == 1

    def test_an_edited_title_is_what_gets_published(self, client, ctx, youtube) -> None:
        job = _parked_job(ctx)
        token = _csrf(client)

        client.post(
            f"/reviews/{job.job_id}/approve",
            data={"csrf_token": token, "title": "A better title", "description": "Nice one"},
            follow_redirects=False,
        )

        assert youtube.uploads[0]["title"] == "A better title"
        assert "Nice one" in youtube.uploads[0]["description"]


class TestMediaRoute:
    def test_serves_the_composed_video(self, client, ctx) -> None:
        job = _parked_job(ctx)

        response = client.get(f"/media/{job.job_id}/short.mp4")

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"

    @pytest.mark.parametrize(
        "attack",
        ["../../../../Windows/win.ini", "..%2f..%2fsecrets.txt", "....//secrets.txt"],
    )
    def test_path_traversal_is_refused(self, client, ctx, attack: str) -> None:
        job = _parked_job(ctx)

        response = client.get(f"/media/{job.job_id}/{attack}")

        assert response.status_code in (400, 404)

    def test_missing_file_is_404(self, client, ctx) -> None:
        job = _parked_job(ctx)
        assert client.get(f"/media/{job.job_id}/absent.mp4").status_code == 404


def test_context_factory_is_wired(settings) -> None:
    """The real app builds its own context; only the test overrides it."""
    app = create_app(settings)
    assert isinstance(app.state.context_factory(), PipelineContext)


def _renderable_job(ctx, **kwargs) -> Job:
    """A parked job whose primary image actually exists, so a re-render can run."""
    job = _parked_job(ctx, **kwargs)
    job.media.primary_image = "pic.png"
    job.thumbnail_hooks = ["GOLDEN HOUR", "THAT SKY", "UNREAL LIGHT"]
    job.thumbnail_text = "GOLDEN HOUR"
    job.thumbnail_direction = {
        "emphasis": "GOLDEN",
        "accent_hex": "#FFD400",
        "text_position": "top",
    }
    (ctx.media_store.job_dir(job.job_id) / "pic.png").write_bytes(make_png(800, 600))
    ctx.job_store.save(job)
    return job


class TestThumbnailReRender:
    """Picking a different hook re-renders, and invalidates the video with it."""

    def test_it_replaces_the_thumbnail_and_records_the_text(self, client, ctx) -> None:
        job = _renderable_job(ctx)
        before = (ctx.media_store.job_dir(job.job_id) / "thumbnail_wide.jpg").read_bytes()
        token = _csrf(client)

        response = client.post(
            f"/reviews/{job.job_id}/thumbnail",
            data={"csrf_token": token, "hook": "THAT SKY"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        after = (ctx.media_store.job_dir(job.job_id) / "thumbnail_wide.jpg").read_bytes()
        assert after != before
        assert ctx.job_store.load(job.job_id).thumbnail_text == "THAT SKY"

    def test_it_invalidates_the_composed_video(self, client, ctx) -> None:
        # The thumbnail is spliced onto both ends of the Short. Without clearing
        # the compose record the uploaded thumbnail and the video's own bumpers
        # would disagree -- silently, and only visible after publishing.
        job = _renderable_job(ctx)
        token = _csrf(client)

        client.post(
            f"/reviews/{job.job_id}/thumbnail",
            data={"csrf_token": token, "hook": "THAT SKY"},
        )

        reloaded = ctx.job_store.load(job.job_id)
        assert "compose" not in reloaded.stages
        assert reloaded.media.composed_video is None
        # Everything before compose survives -- a re-render must not re-ingest.
        assert "ingest" in reloaded.stages
        assert "thumbnail" in reloaded.stages

    def test_a_custom_text_wins_over_the_chosen_hook(self, client, ctx) -> None:
        job = _renderable_job(ctx)
        token = _csrf(client)

        client.post(
            f"/reviews/{job.job_id}/thumbnail",
            data={"csrf_token": token, "hook": "THAT SKY", "custom": "MY OWN WORDS"},
        )

        assert ctx.job_store.load(job.job_id).thumbnail_text == "MY OWN WORDS"

    def test_empty_text_changes_nothing(self, client, ctx) -> None:
        job = _renderable_job(ctx)
        token = _csrf(client)

        client.post(
            f"/reviews/{job.job_id}/thumbnail",
            data={"csrf_token": token, "hook": "", "custom": "   "},
        )

        reloaded = ctx.job_store.load(job.job_id)
        assert reloaded.thumbnail_text == "GOLDEN HOUR"
        assert "compose" in reloaded.stages

    def test_without_a_token_it_is_refused(self, client, ctx) -> None:
        job = _renderable_job(ctx)

        response = client.post(f"/reviews/{job.job_id}/thumbnail", data={"hook": "THAT SKY"})

        assert response.status_code == 403
        assert ctx.job_store.load(job.job_id).thumbnail_text == "GOLDEN HOUR"

    def test_an_already_published_job_is_refused(self, client, ctx) -> None:
        # Swapping the thumbnail of a video already on YouTube would leave the
        # record disagreeing with what is public.
        job = _renderable_job(ctx)
        job.state = JobState.published
        ctx.job_store.save(job)
        token = _csrf(client)

        response = client.post(
            f"/reviews/{job.job_id}/thumbnail",
            data={"csrf_token": token, "hook": "THAT SKY"},
        )

        assert response.status_code == 409
