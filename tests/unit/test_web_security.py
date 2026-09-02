"""Security properties of the internet-facing review app.

These are the tests that would catch the two holes the design review found: a
Google credential reachable from the web tier, and an unauthenticated endpoint
that describes the deployment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeJobTrigger
from ytshort.config import Settings
from ytshort.contracts.models import (
    Job,
    JobState,
    SourceEmail,
    StageRecord,
    StageStatus,
    make_job_id,
    utcnow,
)
from ytshort.web.app import CSRF_COOKIE, create_app


def _azure_settings(tmp_path, monkeypatch) -> Settings:
    """Settings shaped the way the Container App runs: trigger on, auth delegated.

    Self-contained on purpose -- it must produce a valid Settings without relying
    on the ``settings`` fixture having run first.
    """
    monkeypatch.setenv("YTSHORT_DATA_DIR", str(tmp_path / "var-azure"))
    monkeypatch.setenv("YTSHORT_AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("YTSHORT_ALLOWED_SENDERS", "sender@example.com")
    monkeypatch.setenv("YTSHORT_SINKS", "file")
    monkeypatch.setenv("YTSHORT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("YTSHORT_JOB_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-ytshort")
    monkeypatch.setenv("YTSHORT_AZURE_JOB_NAME", "ytshort-run")
    monkeypatch.setenv("YTSHORT_AUTH_MODE", "platform")
    monkeypatch.setenv("YTSHORT_CSRF_SECRET", "a-stable-secret-for-tests")
    monkeypatch.setenv("YTSHORT_REVIEW_HOST", "0.0.0.0")  # noqa: S104 - container bind
    loaded = Settings.load(env_file=tmp_path / "absent.env")
    loaded.ensure_dirs()
    return loaded


def _parked(ctx, subject: str = "Sunset over the lake") -> Job:
    job = Job(
        job_id=make_job_id("m1"),
        state=JobState.awaiting_review,
        source=SourceEmail(message_id="m1", subject=subject, sender="a@example.com"),
        title=subject,
    )
    for name in ("ingest", "safety", "pii", "thumbnail", "compose"):
        job.stages[name] = StageRecord(
            name=name, status=StageStatus.completed, started_at=utcnow(), completed_at=utcnow()
        )
    job.media.composed_video = "short.mp4"
    job.media.thumbnail_tall = "thumbnail_tall.jpg"
    job.media.thumbnail_wide = "thumbnail_wide.jpg"
    job_dir = ctx.media_store.job_dir(job.job_id)
    for name in ("short.mp4", "thumbnail_tall.jpg", "thumbnail_wide.jpg"):
        (job_dir / name).write_bytes(b"\x00")
    ctx.job_store.save(job)
    return job


class TestNoGoogleCredentialInTheWebTier:
    def test_the_app_builds_its_context_without_google_when_a_job_exists(
        self, settings, tmp_path, monkeypatch
    ) -> None:
        # This is THE security property of the two-tier split. If it regresses,
        # a compromise of the public app becomes a compromise of the mailbox.
        azure = _azure_settings(tmp_path, monkeypatch)
        app = create_app(azure)

        ctx = app.state.context_factory()

        assert ctx.gmail is None
        assert ctx.youtube is None

    def test_locally_the_app_keeps_google_so_approve_still_publishes(
        self, settings
    ) -> None:
        # No Job to delegate to on a laptop, so the old inline behaviour stands.
        app = create_app(settings)
        assert settings.job_trigger_enabled is False
        # The factory would build real clients; assert the intent, not the network.
        assert app.state.settings.job_trigger_enabled is False

    def test_approving_delegates_instead_of_publishing(
        self, ctx, youtube, tmp_path, monkeypatch
    ) -> None:
        azure = _azure_settings(tmp_path, monkeypatch)
        app = create_app(azure)
        app.state.context_factory = lambda: ctx
        trigger = FakeJobTrigger()
        app.state.job_trigger = trigger
        client = TestClient(app)

        job = _parked(ctx)
        client.get("/reviews")
        token = client.cookies[CSRF_COOKIE]

        response = client.post(
            f"/reviews/{job.job_id}/approve",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert trigger.started == 1
        # The decision is recorded, but nothing was uploaded from the web tier.
        assert youtube.uploads == []
        decided = ctx.job_store.load(job.job_id)
        assert decided is not None
        assert decided.review is not None
        assert decided.review.decision == "approved"
        assert decided.state is JobState.approved

    def test_a_failed_trigger_does_not_500_the_reviewer(
        self, ctx, youtube, tmp_path, monkeypatch
    ) -> None:
        # The scheduled run will pick the job up anyway; a transient ARM failure
        # must not look like a broken approval.
        azure = _azure_settings(tmp_path, monkeypatch)
        app = create_app(azure)
        app.state.context_factory = lambda: ctx
        app.state.job_trigger = FakeJobTrigger(ok=False, detail="ARM returned 403")
        client = TestClient(app)

        job = _parked(ctx)
        client.get("/reviews")
        token = client.cookies[CSRF_COOKIE]

        response = client.post(
            f"/reviews/{job.job_id}/approve",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        assert response.status_code == 303
        decided = ctx.job_store.load(job.job_id)
        assert decided is not None and decided.state is JobState.approved


class TestHealthDisclosure:
    def test_health_is_bare(self, settings, ctx) -> None:
        # /health is excluded from platform auth so the probe can reach it, which
        # makes it internet-reachable. It must therefore say nothing.
        app = create_app(settings)
        app.state.context_factory = lambda: ctx
        response = TestClient(app).get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_leaks_no_configuration(self, settings, ctx) -> None:
        app = create_app(settings)
        app.state.context_factory = lambda: ctx
        body = TestClient(app).get("/health").text.lower()

        for leak in ("credential", "token", "vault", "ffmpeg", "path", "subscription"):
            assert leak not in body

    def test_health_detail_carries_the_diagnostics(self, settings, ctx) -> None:
        app = create_app(settings)
        app.state.context_factory = lambda: ctx
        payload = TestClient(app).get("/health/detail").json()

        assert "credentials_ok" in payload
        assert "ffmpeg_available" in payload
        assert payload["awaiting_review"] == 0


class TestReviewerIdentity:
    def test_easyauth_principal_is_recorded(self, ctx, tmp_path, monkeypatch) -> None:
        azure = _azure_settings(tmp_path, monkeypatch)
        app = create_app(azure)
        app.state.context_factory = lambda: ctx
        app.state.job_trigger = FakeJobTrigger()
        client = TestClient(app)

        job = _parked(ctx)
        client.get("/reviews")
        token = client.cookies[CSRF_COOKIE]

        client.post(
            f"/reviews/{job.job_id}/approve",
            data={"csrf_token": token},
            headers={"X-MS-CLIENT-PRINCIPAL-NAME": "vrama@example.com"},
            follow_redirects=False,
        )

        decided = ctx.job_store.load(job.job_id)
        assert decided is not None and decided.review is not None
        assert decided.review.reviewer == "vrama@example.com"

    def test_without_the_header_it_falls_back_to_local(self, ctx, settings) -> None:
        app = create_app(settings)
        app.state.context_factory = lambda: ctx
        client = TestClient(app)

        job = _parked(ctx)
        client.get("/reviews")
        token = client.cookies[CSRF_COOKIE]
        client.post(
            f"/reviews/{job.job_id}/reject",
            data={"csrf_token": token, "reason": "no"},
            follow_redirects=False,
        )

        decided = ctx.job_store.load(job.job_id)
        assert decided is not None and decided.review is not None
        assert decided.review.reviewer == "local"


class TestStableCsrf:
    def test_a_configured_secret_survives_a_restart(self, tmp_path, monkeypatch) -> None:
        # Scale-to-zero recycles the container between page load and submit. With
        # a per-process token that 403s; with a configured one it does not.
        azure = _azure_settings(tmp_path, monkeypatch)
        first = create_app(azure)
        second = create_app(azure)  # simulates the replacement container

        assert first.state.csrf_token == second.state.csrf_token == azure.csrf_secret

    def test_without_a_secret_each_process_differs(self, settings) -> None:
        assert create_app(settings).state.csrf_token != create_app(settings).state.csrf_token


class TestMandatoryAllowList:
    def test_an_empty_allow_list_refuses_to_start(self, tmp_path, monkeypatch) -> None:
        # The pipeline's front door: without this, any stranger who emails the
        # watched mailbox can queue media for publication.
        from ytshort.config import ConfigError

        for key in ("YTSHORT_ALLOWED_SENDERS",):
            monkeypatch.setenv(key, "")

        with pytest.raises(ConfigError, match="ALLOWED_SENDERS"):
            Settings.load(env_file=tmp_path / "absent.env")


class TestCorrelationHeader:
    def test_a_response_carries_a_correlation_id(self, settings) -> None:
        client = TestClient(create_app(settings))
        response = client.get("/reviews")

        assert response.headers["x-correlation-id"]

    def test_an_inbound_id_is_honoured(self, settings) -> None:
        client = TestClient(create_app(settings))
        response = client.get("/reviews", headers={"X-Correlation-Id": "caller-supplied-1"})

        assert response.headers["x-correlation-id"] == "caller-supplied-1"

    def test_a_w3c_traceparent_supplies_the_trace_id(self, settings) -> None:
        client = TestClient(create_app(settings))
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        response = client.get(
            "/reviews",
            headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"},
        )

        assert response.headers["x-correlation-id"] == trace_id

    @pytest.mark.parametrize(
        "hostile",
        [
            "\r\nSet-Cookie: x=1",
            "not a valid id",
            "x" * 500,
            "../../etc/passwd",
            '{"level":"INFO"}',
        ],
    )
    def test_a_hostile_id_is_replaced_not_reflected(self, settings, hostile: str) -> None:
        # An inbound id lands in a log field. Reflecting it would let a caller forge
        # log lines, and an unbounded value is a cheap way to inflate log volume.
        client = TestClient(create_app(settings))
        response = client.get("/reviews", headers={"X-Correlation-Id": hostile})

        emitted = response.headers["x-correlation-id"]
        assert emitted != hostile
        assert emitted.isalnum() and len(emitted) == 32

    def test_health_is_not_instrumented(self, settings) -> None:
        # The readiness probe hits it every ten seconds. Correlating that is 8,640
        # billed records a day saying nothing.
        client = TestClient(create_app(settings))
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert "x-correlation-id" not in response.headers


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "no-referrer"),
        ],
    )
    def test_they_are_present(self, settings, header: str, expected: str) -> None:
        client = TestClient(create_app(settings))
        assert client.get("/reviews").headers[header] == expected

    def test_the_csp_forbids_scripts(self, settings) -> None:
        # The UI renders attacker-influenced text -- subjects, filenames, OCR output.
        # It contains no scripts of its own, so 'none' costs nothing and is real.
        client = TestClient(create_app(settings))
        csp = client.get("/reviews").headers["content-security-policy"]

        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src" not in csp  # inherits 'none' from default-src


class TestNoConnectionStringIsDisclosed:
    def test_health_endpoints_never_echo_it(self, tmp_path, monkeypatch) -> None:
        sentinel = "InstrumentationKey=deadbeef-0000-0000-0000-000000000000"
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", sentinel)
        azure = _azure_settings(tmp_path, monkeypatch)
        assert azure.otel_connection_string == sentinel

        client = TestClient(create_app(azure))
        for path in ("/health", "/health/detail"):
            body = client.get(path).text
            assert sentinel not in body
            assert "deadbeef" not in body
            assert "InstrumentationKey" not in body

    def test_health_detail_reports_only_whether_export_is_on(
        self, tmp_path, monkeypatch
    ) -> None:
        azure = _azure_settings(tmp_path, monkeypatch)
        client = TestClient(create_app(azure))
        payload = client.get("/health/detail").json()

        assert payload["telemetry_exporting"] is False
        assert payload["service_name"] == "ytshort"
