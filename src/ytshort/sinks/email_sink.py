"""Email the short URL, reusing the Gmail credential the pipeline already holds.

Sending through the Gmail API rather than SMTP avoids a second credential (no app
password to store) and keeps the whole Google surface on one OAuth grant.
"""

from __future__ import annotations

import html

from ytshort.contracts.models import Job, SinkResult
from ytshort.observability.logging import get_logger
from ytshort.pipeline.stage import PipelineContext
from ytshort.sinks.base import message_for

log = get_logger(__name__)


class EmailSink:
    name = "email"

    def deliver(self, job: Job, ctx: PipelineContext) -> SinkResult:
        assert job.publication is not None
        delivery_id = job.delivery_id_for(self.name)
        recipients = list(ctx.settings.email_recipients)

        if not recipients:
            return SinkResult(
                sink=self.name,
                delivery_id=delivery_id,
                ok=False,
                detail="no recipients configured (YTSHORT_EMAIL_RECIPIENTS)",
            )
        if ctx.gmail is None:
            return SinkResult(
                sink=self.name,
                delivery_id=delivery_id,
                ok=False,
                detail="no Gmail client available to send with",
            )

        subject, body = message_for(job)
        message_id = ctx.gmail.send_message(
            to=recipients,
            subject=subject,
            body_text=body,
            body_html=self._html(job, body),
        )

        log.info("notification emailed", extra={"recipients": len(recipients)})
        return SinkResult(
            sink=self.name,
            delivery_id=delivery_id,
            ok=True,
            detail=f"sent to {len(recipients)} recipient(s), gmail id {message_id}",
        )

    def _html(self, job: Job, body: str) -> str:
        assert job.publication is not None
        url = html.escape(job.publication.short_url)
        # Everything interpolated here originates in an email we received, so it
        # all goes through html.escape -- a subject containing markup must render
        # as text, not as markup.
        return (
            "<div style=\"font-family:system-ui,-apple-system,Segoe UI,sans-serif\">"
            f"<h2 style=\"margin:0 0 8px\">{html.escape(job.title)}</h2>"
            f'<p><a href="{url}">{url}</a></p>'
            f"<pre style=\"white-space:pre-wrap;color:#444;font-size:13px\">"
            f"{html.escape(body)}</pre>"
            "</div>"
        )
