"""In-memory stand-ins for every external dependency.

These implement the same protocols as the real adapters, which is what lets the
whole pipeline -- ingest through publish through fan-out -- run in a test with no
network, no Google project, and no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ytshort.integrations.gmail_client import GmailAttachment, GmailMessage
from ytshort.integrations.moderation import ModerationResult
from ytshort.integrations.scanner import ScanResult
from ytshort.integrations.youtube_client import UploadResult


@dataclass
class FakeGmailClient:
    """Read and send only -- mirrors the narrowed GmailClientProtocol.

    There is deliberately no labelling method: the app no longer holds
    gmail.modify, and a fake that offers more than the real client would let a
    test pass against capability we do not have.
    """

    messages: dict[str, GmailMessage] = field(default_factory=dict)
    attachments: dict[tuple[str, str], bytes] = field(default_factory=dict)
    sent: list[dict] = field(default_factory=list)
    download_error: Exception | None = None

    def add_message(
        self,
        message_id: str,
        *,
        subject: str = "A subject",
        sender: str = "sender@example.com",
        snippet: str = "",
        files: dict[str, bytes] | None = None,
        mime_overrides: dict[str, str] | None = None,
        declared_sizes: dict[str, int] | None = None,
    ) -> GmailMessage:
        files = files or {}
        mime_overrides = mime_overrides or {}
        declared_sizes = declared_sizes or {}

        parts = []
        for index, (filename, data) in enumerate(files.items()):
            attachment_id = f"{message_id}-att{index}"
            parts.append(
                GmailAttachment(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=mime_overrides.get(filename, "application/octet-stream"),
                    size_bytes=declared_sizes.get(filename, len(data)),
                )
            )
            self.attachments[(message_id, attachment_id)] = data

        message = GmailMessage(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            subject=subject,
            sender=sender,
            snippet=snippet,
            received_at=datetime.now(UTC),
            attachments=parts,
        )
        self.messages[message_id] = message
        return message

    # -- protocol ----------------------------------------------------------
    def list_message_ids(self, query: str, max_results: int) -> list[str]:
        return list(self.messages)[:max_results]

    def get_message(self, message_id: str) -> GmailMessage:
        return self.messages[message_id]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if self.download_error is not None:
            raise self.download_error
        return self.attachments[(message_id, attachment_id)]

    def send_message(
        self, to: list[str], subject: str, body_text: str, body_html: str | None = None
    ) -> str:
        self.sent.append(
            {"to": to, "subject": subject, "body_text": body_text, "body_html": body_html}
        )
        return f"sent-{len(self.sent)}"


@dataclass
class FakeYouTubeClient:
    uploads: list[dict] = field(default_factory=list)
    thumbnails: list[tuple[str, str]] = field(default_factory=list)
    visibility_calls: list[tuple[str, str]] = field(default_factory=list)
    #: What YouTube actually applies -- set to "private" to simulate an
    #: unaudited API project overriding the request.
    forced_privacy: str | None = None
    thumbnail_succeeds: bool = True
    upload_error: Exception | None = None

    def upload_video(
        self,
        path: Path,
        *,
        title: str,
        description: str,
        tags: list[str],
        category_id: str,
        privacy_status: str,
    ) -> UploadResult:
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append(
            {
                "path": str(path),
                "title": title,
                "description": description,
                "tags": tags,
                "category_id": category_id,
                "privacy_status": privacy_status,
            }
        )
        return UploadResult(
            video_id=f"vid{len(self.uploads):04d}",
            uploaded_privacy_status=self.forced_privacy or privacy_status,
        )

    def set_thumbnail(self, video_id: str, path: Path) -> bool:
        self.thumbnails.append((video_id, str(path)))
        return self.thumbnail_succeeds

    def set_visibility(self, video_id: str, privacy_status: str) -> str:
        self.visibility_calls.append((video_id, privacy_status))
        # forced_privacy models an unaudited project refusing to go public.
        return self.forced_privacy or privacy_status


@dataclass
class FakeJobTrigger:
    """Stands in for the ARM call that starts the scheduled Job."""

    started: int = 0
    ok: bool = True
    detail: str = "execution started"

    def start(self):
        from ytshort.integrations.job_trigger import TriggerResult

        self.started += 1
        return TriggerResult(ok=self.ok, detail=self.detail)


@dataclass
class FakeScanner:
    name: str = "fake"
    clean: bool = True
    skipped: bool = False
    detail: str = "no threats found"

    def scan(self, path: Path) -> ScanResult:
        return ScanResult(
            clean=self.clean, provider=self.name, detail=self.detail, skipped=self.skipped
        )


@dataclass
class FakeModerator:
    name: str = "fake"
    flagged: bool = False
    skipped: bool = False
    categories: list[str] = field(default_factory=list)
    detail: str = "looks fine"

    def moderate(self, path: Path) -> ModerationResult:
        return ModerationResult(
            flagged=self.flagged,
            provider=self.name,
            categories=list(self.categories),
            detail=self.detail,
            skipped=self.skipped,
        )


@dataclass
class RecordingInstrument:
    """Stands in for a ccol metric instrument and remembers what was measured.

    The real instrument is a no-op unless telemetry is configured, so asserting on
    emitted metrics means substituting this for the accessor in
    ``ytshort.observability.instruments``.
    """

    points: list[tuple[float, dict]] = field(default_factory=list)

    def add(self, value: int | float, attributes: dict | None = None) -> None:
        self.points.append((value, attributes or {}))

    def record(self, value: int | float, attributes: dict | None = None) -> None:
        self.add(value, attributes)

    def attributes_for(self, key: str) -> list:
        return [attrs.get(key) for _, attrs in self.points]
