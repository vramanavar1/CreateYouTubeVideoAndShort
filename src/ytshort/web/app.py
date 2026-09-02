"""FastAPI app for the human review gate.

Deliberate v1 limitations, stated rather than hidden:

* It binds to 127.0.0.1 and has **no authentication**. Anyone who can reach the
  port can approve a publication, so it must not be exposed on a network
  interface without an authenticating proxy in front.
* CSRF is handled with a double-submit token, which is the part that actually
  matters on localhost: without it, any web page you happen to have open could
  POST an approval to ``127.0.0.1:8080`` in the background.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI

from ytshort.config import Settings
from ytshort.integrations.job_trigger import build_job_trigger
from ytshort.observability.logging import get_logger
from ytshort.runtime import build_context

log = get_logger(__name__)

CSRF_COOKIE = "ytshort_csrf"


def create_app(settings: Settings | None = None) -> FastAPI:
    from ytshort.web.routes_review import build_router

    settings = settings or Settings.load(strict=False)
    settings.ensure_dirs()

    app = FastAPI(
        title="ytshort review",
        docs_url=None,       # no API docs surface -- nothing to enumerate
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    # A configured secret survives restarts and scale-to-zero, so an open form
    # still submits after the container has been recycled. Falling back to a
    # per-process random value keeps local development zero-config.
    app.state.csrf_token = settings.csrf_secret or secrets.token_urlsafe(32)
    if not settings.csrf_secret and settings.auth_mode == "platform":
        log.warning(
            "no YTSHORT_CSRF_SECRET set; forms will break across restarts and "
            "scale-to-zero. Wire the Key Vault secret in."
        )

    # When a Job exists to delegate to (the Azure deployment), this app is built
    # WITHOUT Google clients -- the internet-facing tier must never hold a
    # credential that can read mail or upload video. Locally there is no Job, so
    # it keeps the clients and publishes inline exactly as before.
    app.state.context_factory = lambda: build_context(
        settings, with_google=not settings.job_trigger_enabled
    )
    app.state.job_trigger = build_job_trigger(settings)

    app.include_router(build_router())
    return app


def serve(settings: Settings | None = None) -> None:
    import uvicorn

    settings = settings or Settings.load(strict=False)
    app = create_app(settings)

    # Binding off loopback is only safe when something in front is authenticating.
    # In Azure that is Container Apps EasyAuth, declared by auth_mode=platform.
    if settings.review_host not in ("127.0.0.1", "localhost", "::1"):
        if settings.auth_mode == "platform":
            log.info(
                "binding off loopback; authentication is enforced by the platform",
                extra={"host": settings.review_host},
            )
        else:
            log.warning(
                "review UI is binding to a non-loopback address with NO authentication. "
                "Set YTSHORT_AUTH_MODE=platform only when a gateway authenticates for it.",
                extra={"host": settings.review_host},
            )

    log.info(
        "review UI starting",
        extra={"url": f"http://{settings.review_host}:{settings.review_port}/reviews"},
    )
    uvicorn.run(app, host=settings.review_host, port=settings.review_port, log_level="warning")
