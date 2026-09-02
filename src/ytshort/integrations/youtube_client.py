"""YouTube Data API v3 adapter: resumable upload plus custom thumbnail.

Two constraints this module has to live with, both external and neither fixable
in code:

* An API project created after 2020-07-28 that has not passed YouTube's
  compliance audit has **every upload force-locked to private**, whatever
  ``privacyStatus`` the request asks for. ``uploaded_privacy_status`` reports what
  YouTube actually did so the pipeline can tell the truth rather than the request.
* ``thumbnails.set`` requires a phone-verified channel and returns 403 otherwise.
  That must not fail an otherwise successful publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

# Upload in 5 MB chunks so a dropped connection loses a chunk, not the whole file.
_CHUNK_SIZE = 5 * 1024 * 1024


@dataclass
class UploadResult:
    video_id: str
    uploaded_privacy_status: str


class YouTubeClientProtocol(Protocol):
    def upload_video(
        self,
        path: Path,
        *,
        title: str,
        description: str,
        tags: list[str],
        category_id: str,
        privacy_status: str,
    ) -> UploadResult: ...

    def set_thumbnail(self, video_id: str, path: Path) -> bool: ...

    def set_visibility(self, video_id: str, privacy_status: str) -> str: ...


class YouTubeClient:
    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def build(cls, settings, *, allow_interactive: bool = False) -> YouTubeClient:
        from googleapiclient.discovery import build

        from ytshort.integrations.google_auth import load_credentials

        creds = load_credentials(settings, allow_interactive=allow_interactive)
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return cls(service)

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
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(
            str(path), mimetype="video/mp4", chunksize=_CHUNK_SIZE, resumable=True
        )
        request = self._service.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    # YouTube truncates past 100 chars; do it ourselves so the
                    # title we record matches the title that exists.
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": tags[:30],
                    "categoryId": category_id,
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            },
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info("upload progress", extra={"percent": int(status.progress() * 100)})

        video_id = response["id"]
        actual = (response.get("status", {}) or {}).get("privacyStatus", privacy_status)

        if actual != privacy_status:
            log.warning(
                "youtube overrode the requested privacy status "
                "(unaudited API projects are force-locked to private)",
                extra={"requested": privacy_status, "actual": actual},
            )

        return UploadResult(video_id=video_id, uploaded_privacy_status=actual)

    def set_thumbnail(self, video_id: str, path: Path) -> bool:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        try:
            self._service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(path), mimetype="image/jpeg"),
            ).execute()
            return True
        except HttpError as exc:
            # 403 here almost always means the channel is not phone-verified.
            log.warning(
                "custom thumbnail rejected",
                extra={"video_id": video_id, "status": exc.status_code, "error": str(exc)},
            )
            return False

    def set_visibility(self, video_id: str, privacy_status: str) -> str:
        """Change a published video's visibility. Returns what YouTube applied.

        Used to promote the backlog of private uploads once the compliance audit
        clears. This needs the ``youtube.force-ssl`` scope -- ``youtube.upload``
        alone cannot call ``videos.update``.

        ``videos.update`` replaces the whole ``status`` part, so the current
        status is read first and only ``privacyStatus`` altered; otherwise
        properties like ``selfDeclaredMadeForKids`` are silently reset.
        """
        current = (
            self._service.videos().list(part="status", id=video_id).execute()
        )
        items = current.get("items", [])
        if not items:
            raise ValueError(f"no video found with id {video_id}")

        status = dict(items[0].get("status", {}))
        status["privacyStatus"] = privacy_status
        # Read-only fields are rejected on write.
        for read_only in ("uploadStatus", "failureReason", "rejectionReason", "publishAt"):
            status.pop(read_only, None)

        response = (
            self._service.videos()
            .update(part="status", body={"id": video_id, "status": status})
            .execute()
        )
        applied = (response.get("status", {}) or {}).get("privacyStatus", privacy_status)
        log.info(
            "visibility updated",
            extra={"video_id": video_id, "requested": privacy_status, "applied": applied},
        )
        return applied
