"""Review queue, job detail, media preview, and the approve/reject endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ytshort.contracts.models import Job, JobState
from ytshort.observability.logging import get_logger
from ytshort.runtime import record_decision, resume_job
from ytshort.web.app import CSRF_COOKIE

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

#: States a reviewer can still act on, newest first in the queue.
_QUEUE_ORDER = (
    JobState.awaiting_review,
    JobState.approved,
    JobState.published,
    JobState.done,
    JobState.rejected,
    JobState.quarantined,
    JobState.failed,
)


def _check_csrf(request: Request, token: str) -> None:
    """Double-submit: the form value must match both the cookie and the app token.

    A cross-origin page can force a POST but cannot read the cookie to populate
    the form field, so the mismatch stops it.
    """
    expected = request.app.state.csrf_token
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not token or token != expected or cookie != expected:
        raise HTTPException(status_code=403, detail="CSRF token mismatch; reload the page")


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, context)
    response.set_cookie(
        CSRF_COOKIE,
        request.app.state.csrf_token,
        httponly=False,  # the form reads it back; that is the double-submit design
        samesite="strict",
        secure=False,  # localhost is plain HTTP; gate this on an https deployment
    )
    return response


def _reviewer(request: Request) -> str:
    """Who is approving. EasyAuth injects the signed-in principal.

    Container Apps sets this header after authenticating; it cannot be spoofed
    from outside because the platform strips client-supplied copies. Locally
    there is no header and no auth, so it falls back to "local".
    """
    name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "").strip()
    return name[:120] if name else "local"


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/reviews")

    @router.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe. Deliberately says nothing.

        This endpoint is excluded from platform authentication so the probe can
        reach it, which makes it internet-reachable without credentials. It must
        therefore disclose nothing about configuration, credentials, or
        environment -- see /health/detail for the diagnostics.
        """
        return {"status": "ok"}

    @router.get("/health/detail")
    def health_detail(request: Request) -> dict[str, object]:
        """Diagnostics. Behind authentication, unlike /health."""
        from ytshort.integrations.ffmpeg import FFmpeg
        from ytshort.integrations.google_auth import describe_credentials

        settings = request.app.state.settings
        ctx = request.app.state.context_factory()
        ffmpeg = FFmpeg.from_settings(settings)
        credentials = describe_credentials(settings)

        return {
            "status": "ok",
            "reviewer": _reviewer(request),
            "ffmpeg_available": ffmpeg.available,
            "credentials_ok": credentials.ok,
            "credential_source": credentials.source,
            "awaiting_review": len(ctx.job_store.list_jobs(JobState.awaiting_review)),
            "job_trigger_enabled": settings.job_trigger_enabled,
        }

    @router.get("/reviews", response_class=HTMLResponse)
    def queue(request: Request) -> HTMLResponse:
        ctx = request.app.state.context_factory()
        jobs = list(ctx.job_store.iter_jobs())
        rank = {state: index for index, state in enumerate(_QUEUE_ORDER)}
        jobs.sort(key=lambda j: (rank.get(j.state, 99), -j.created_at.timestamp()))

        return _render(
            request,
            "queue.html",
            {
                "jobs": jobs,
                "awaiting": [j for j in jobs if j.state is JobState.awaiting_review],
                "csrf_token": request.app.state.csrf_token,
            },
        )

    @router.get("/reviews/{job_id}", response_class=HTMLResponse)
    def detail(request: Request, job_id: str) -> HTMLResponse:
        ctx = request.app.state.context_factory()
        job = ctx.job_store.load(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")

        return _render(
            request,
            "detail.html",
            {
                "job": job,
                "can_decide": job.state is JobState.awaiting_review,
                "csrf_token": request.app.state.csrf_token,
            },
        )

    @router.get("/media/{job_id}/{filename}")
    def media(request: Request, job_id: str, filename: str) -> FileResponse:
        ctx = request.app.state.context_factory()
        try:
            # resolve() refuses anything that escapes the job directory, so a
            # traversal in `filename` cannot read arbitrary files.
            path = ctx.media_store.resolve(job_id, filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc

        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")

        return FileResponse(
            path,
            media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        )

    @router.post("/reviews/{job_id}/approve")
    def approve(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        title: str = Form(""),
        description: str = Form(""),
        tags: str = Form(""),
    ) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        ctx = request.app.state.context_factory()
        job = _load_decidable(ctx, job_id)

        # Reviewer edits win over whatever the pipeline derived.
        if title.strip():
            job.title = title.strip()[:100]
        if description.strip():
            job.description = description.strip()
        if tags.strip():
            job.tags = [t.strip() for t in tags.split(",") if t.strip()]

        reviewer = _reviewer(request)
        record_decision(job, ctx, decision="approved", reviewer=reviewer)
        log.info("approved via review UI", extra={"job": job_id, "reviewer": reviewer})

        # Publishing happens in the scheduled Job, never here. This app holds no
        # Google credential by design, so all it can do is ask the Job to run now
        # instead of at the next cron tick.
        settings = request.app.state.settings
        if settings.job_trigger_enabled:
            result = request.app.state.job_trigger.start()
            if result.ok:
                log.info("triggered the publish job", extra={"detail": result.detail})
            else:
                # Not something the reviewer must act on -- the scheduled run will
                # pick the approved job up regardless. Worth a log line, not a 500.
                log.warning(
                    "could not trigger the publish job; the next scheduled run will "
                    "handle it",
                    extra={"detail": result.detail},
                )
        else:
            # Local development: there is no Job to trigger, and this context does
            # carry Google clients, so publish inline as before.
            resume_job(job_id, ctx)

        return RedirectResponse(f"/reviews/{job_id}", status_code=303)

    @router.post("/reviews/{job_id}/reject")
    def reject(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        reason: str = Form(""),
    ) -> RedirectResponse:
        _check_csrf(request, csrf_token)
        ctx = request.app.state.context_factory()
        job = _load_decidable(ctx, job_id)

        reviewer = _reviewer(request)
        record_decision(
            job, ctx, decision="rejected", reviewer=reviewer, reason=reason.strip()
        )
        log.info("rejected via review UI", extra={"job": job_id, "reviewer": reviewer})
        return RedirectResponse(f"/reviews/{job_id}", status_code=303)

    return router


def _load_decidable(ctx, job_id: str) -> Job:
    job = ctx.job_store.load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.state is not JobState.awaiting_review:
        raise HTTPException(
            status_code=409,
            detail=f"job is {job.state}, not awaiting review",
        )
    return job
