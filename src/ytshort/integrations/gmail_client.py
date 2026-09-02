"""Gmail adapter: discover candidate mail, pull attachments, label, and send.

The protocol below is the seam the tests use. ``GmailClient`` is the real
implementation; ``tests/fakes.py`` provides an in-memory stand-in with the same
surface, which is why the whole pipeline can be exercised without a network.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Protocol

from ytshort.observability.logging import get_logger

if TYPE_CHECKING:
    from ytshort.config import Settings

log = get_logger(__name__)


@dataclass
class GmailAttachment:
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class GmailMessage:
    message_id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    snippet: str = ""
    received_at: datetime | None = None
    attachments: list[GmailAttachment] = field(default_factory=list)


class GmailClientProtocol(Protocol):
    """Read and send only.

    There is deliberately no labelling or modification method here: the app holds
    ``gmail.readonly`` and ``gmail.send``, not ``gmail.modify``. Keeping the
    protocol narrow means a future stage cannot quietly reintroduce a need for
    mailbox write access.
    """

    def list_message_ids(self, query: str, max_results: int) -> list[str]: ...
    def get_message(self, message_id: str) -> GmailMessage: ...
    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...
    def send_message(
        self, to: list[str], subject: str, body_text: str, body_html: str | None = None
    ) -> str: ...


def _header(headers: list[dict[str, str]], name: str) -> str:
    lowered = name.lower()
    for header in headers:
        if header.get("name", "").lower() == lowered:
            return header.get("value", "")
    return ""


def _walk_parts(part: dict[str, Any]):
    """Depth-first walk of a Gmail payload tree (multipart nests arbitrarily)."""
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


class GmailClient:
    def __init__(self, service: Any, user_id: str = "me") -> None:
        self._service = service
        self._user_id = user_id

    @classmethod
    def build(cls, settings: Settings, *, allow_interactive: bool = False) -> GmailClient:
        from googleapiclient.discovery import build

        from ytshort.integrations.google_auth import load_credentials

        creds = load_credentials(settings, allow_interactive=allow_interactive)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return cls(service)

    # -- reading -----------------------------------------------------------
    def list_message_ids(self, query: str, max_results: int) -> list[str]:
        response = (
            self._service.users()
            .messages()
            .list(userId=self._user_id, q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in response.get("messages", [])]

    def get_message(self, message_id: str) -> GmailMessage:
        raw = (
            self._service.users()
            .messages()
            .get(userId=self._user_id, id=message_id, format="full")
            .execute()
        )
        payload = raw.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        received_at = None
        if internal := raw.get("internalDate"):
            received_at = datetime.fromtimestamp(int(internal) / 1000, tz=UTC)

        attachments: list[GmailAttachment] = []
        for part in _walk_parts(payload):
            body = part.get("body", {}) or {}
            attachment_id = body.get("attachmentId")
            filename = part.get("filename") or ""
            # Inline images without a filename are not what this pipeline is for;
            # requiring both an id and a name keeps signatures/logos out.
            if attachment_id and filename:
                attachments.append(
                    GmailAttachment(
                        attachment_id=attachment_id,
                        filename=filename,
                        mime_type=part.get("mimeType", "application/octet-stream"),
                        size_bytes=int(body.get("size", 0)),
                    )
                )

        return GmailMessage(
            message_id=message_id,
            thread_id=raw.get("threadId", ""),
            subject=_header(headers, "Subject"),
            sender=_header(headers, "From"),
            snippet=raw.get("snippet", ""),
            received_at=received_at,
            attachments=attachments,
        )

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = (
            self._service.users()
            .messages()
            .attachments()
            .get(userId=self._user_id, messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(response["data"])

    # -- sending -----------------------------------------------------------
    def send_message(
        self, to: list[str], subject: str, body_text: str, body_html: str | None = None
    ) -> str:
        message = EmailMessage()
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message.set_content(body_text)
        if body_html:
            message.add_alternative(body_html, subtype="html")

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = (
            self._service.users()
            .messages()
            .send(userId=self._user_id, body={"raw": encoded})
            .execute()
        )
        return sent.get("id", "")
