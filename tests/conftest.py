"""Shared fixtures. Every test runs against a throwaway data dir and no network."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from tests.fakes import FakeGmailClient, FakeModerator, FakeScanner, FakeYouTubeClient
from ytshort.config import Settings
from ytshort.pipeline.stage import PipelineContext
from ytshort.storage.job_store import JobStore
from ytshort.storage.media_store import MediaStore

# Env vars the loader reads. Cleared before every test so a developer's real
# .env can never leak into a run and, say, point tests at a live inbox.
_ENV_KEYS = [
    "YTSHORT_GOOGLE_CLIENT_SECRET_FILE",
    "YTSHORT_GOOGLE_TOKEN_FILE",
    "YTSHORT_GMAIL_QUERY",
    "YTSHORT_ALLOWED_SENDERS",
    "YTSHORT_MAX_EMAILS_PER_DAY",
    "YTSHORT_MAX_TOTAL_ATTACHMENT_BYTES",
    "YTSHORT_FFMPEG_PATH",
    "YTSHORT_FFPROBE_PATH",
    "YTSHORT_AUDIO_DIR",
    "YTSHORT_BUMPER_SECONDS",
    "YTSHORT_MAX_SHORT_SECONDS",
    "YTSHORT_BACKGROUND_AUDIO_GAIN",
    "YTSHORT_PII_POLICY",
    "YTSHORT_MALWARE_SCANNER",
    "YTSHORT_MODERATION_PROVIDER",
    "ANTHROPIC_API_KEY",
    "YTSHORT_PRIVACY_STATUS",
    "YTSHORT_VIDEO_CATEGORY_ID",
    "YTSHORT_VIDEO_TAGS",
    "YTSHORT_SINKS",
    "YTSHORT_EMAIL_RECIPIENTS",
    "YTSHORT_REVIEW_HOST",
    "YTSHORT_REVIEW_PORT",
    "YTSHORT_DATA_DIR",
    "YTSHORT_LOG_LEVEL",
    "YTSHORT_LOG_FORMAT",
    "YTSHORT_LOG_TO_FILE",
    "YTSHORT_AUTH_MODE",
    "YTSHORT_CSRF_SECRET",
    "YTSHORT_CREDENTIAL_STORE",
    "YTSHORT_KEY_VAULT_URI",
    "YTSHORT_JOB_TRIGGER_ENABLED",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "YTSHORT_AZURE_JOB_NAME",
    "YTSHORT_MEDIA_RETENTION_DAYS",
]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    monkeypatch.setenv("YTSHORT_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("YTSHORT_AUDIO_DIR", str(audio_dir))
    monkeypatch.setenv("YTSHORT_MALWARE_SCANNER", "none")
    monkeypatch.setenv("YTSHORT_MODERATION_PROVIDER", "none")
    monkeypatch.setenv("YTSHORT_SINKS", "file")
    monkeypatch.setenv("YTSHORT_LOG_LEVEL", "WARNING")
    # The allow-list is mandatory, so every test needs one. The fakes send from
    # sender@example.com by default.
    monkeypatch.setenv("YTSHORT_ALLOWED_SENDERS", "sender@example.com")

    # env_file points at nothing, so the repo's real .env is never read.
    loaded = Settings.load(env_file=tmp_path / "absent.env")
    loaded.ensure_dirs()
    return loaded


@pytest.fixture
def gmail() -> FakeGmailClient:
    return FakeGmailClient()


@pytest.fixture
def youtube() -> FakeYouTubeClient:
    return FakeYouTubeClient()


@pytest.fixture
def ctx(settings: Settings, gmail: FakeGmailClient, youtube: FakeYouTubeClient) -> PipelineContext:
    return PipelineContext(
        settings=settings,
        job_store=JobStore(settings.jobs_dir),
        media_store=MediaStore(settings.media_dir),
        gmail=gmail,
        youtube=youtube,
        scanner=FakeScanner(),
        moderator=FakeModerator(),
    )


@pytest.fixture
def png_bytes() -> bytes:
    """A small, real PNG -- magic bytes and all, so the safety gate accepts it."""
    return make_png(640, 480)


def make_png(width: int, height: int, colour: tuple[int, int, int] = (70, 130, 180)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def audio_track(settings: Settings) -> Path:
    """A 3-second silent MP3 when ffmpeg is present, else a placeholder file.

    The placeholder is enough for AudioSource selection tests; anything that
    actually renders is marked ``ffmpeg`` and skipped without the binary.
    """
    target = settings.audio_dir / "track.mp3"
    if ffmpeg_available():
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "3", str(target),
            ],
            check=True,
            capture_output=True,
        )
    else:
        target.write_bytes(b"\x00" * 64)
    return target


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """A real 2-second MP4. Only usable when ffmpeg is installed."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    target = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target
