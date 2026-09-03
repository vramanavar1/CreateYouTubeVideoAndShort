"""Environment-driven configuration, validated once at startup.

Everything the pipeline can be tuned with lives here. Two rules that the rest of
the codebase depends on:

1. No module reads ``os.environ`` directly -- they take a ``Settings``. That is
   what makes the whole pipeline testable without touching the real environment.
2. Validation is eager and *collects* problems rather than failing on the first
   one, so a misconfigured install tells you everything wrong in a single run
   instead of one error per attempt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Imported for the default only; the module has no heavy or optional deps.
from ytshort.integrations.art_director import DEFAULT_API_VERSION

# Repo root is the parent of ``src/``. Relative paths in .env (audio dir, data
# dir) resolve against this, so the CLI behaves the same from any working
# directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Gmail read + label + send, and YouTube upload. Deliberately minimal: no
# gmail.compose and no broad drive scopes.
#
# Deliberately NOT gmail.modify: it grants write over the entire mailbox, and the
# only thing we used it for was applying a "processed" label. Deduplication is
# already handled properly by JobStore.known_message_ids(), so the label was
# redundant -- and a credential that lives in the cloud should not be able to
# delete your mail.
#
# youtube.force-ssl is granted up front even though it is only needed after the
# compliance audit clears (for videos.update to promote private uploads to
# public). Asking for it now avoids a second consent round later.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

#: Container formats the audio selector accepts.
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"})

PrivacyStatus = Literal["private", "unlisted", "public"]
PiiPolicy = Literal["warn", "block"]
AuthMode = Literal["platform", "none"]
CredentialStoreKind = Literal["file", "keyvault"]


class ConfigError(Exception):
    """Raised when the environment cannot produce a usable Settings."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, problems: list[str]) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        problems.append(f"{name} must be an integer, got {raw!r}")
        return default


def _env_float(name: str, default: float, problems: list[str]) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        problems.append(f"{name} must be a number, got {raw!r}")
        return default


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _module_available(name: str) -> bool:
    """Is an optional dependency importable, without importing it?

    ``find_spec`` imports parent packages on the way down, so a missing parent
    raises instead of returning None -- asking for "azure.monitor.opentelemetry"
    on an install without the azure extra would otherwise take out
    ``ytshort doctor``, which is the one command you run when things are broken.
    """
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _resolve(value: str, default: str) -> Path:
    """Resolve a configured path, treating relative values as repo-relative."""
    path = Path(value or default)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    # -- Google auth -------------------------------------------------------
    google_client_secret_file: Path
    google_token_file: Path

    # -- Credential storage ------------------------------------------------
    credential_store: CredentialStoreKind
    key_vault_uri: str
    kv_secret_refresh_token: str
    kv_secret_client_id: str
    kv_secret_client_secret: str

    # -- Gmail ingestion ---------------------------------------------------
    gmail_query: str
    allowed_senders: tuple[str, ...]
    max_emails_per_day: int
    max_total_attachment_bytes: int

    # -- Media -------------------------------------------------------------
    ffmpeg_path: str
    ffprobe_path: str
    audio_dir: Path
    #: Credit line appended to every YouTube description. Empty when the track
    #: needs no attribution, which is the usual case. Some licences make a credit a
    #: condition of use, and an obligation that holds for every upload belongs in
    #: the pipeline rather than in a human's memory.
    audio_credit: str
    bumper_seconds: float
    max_short_seconds: int
    background_audio_gain: float

    # -- Safety ------------------------------------------------------------
    pii_policy: PiiPolicy
    malware_scanner: str
    virustotal_api_key: str
    moderation_provider: str
    anthropic_api_key: str

    # -- Thumbnail art direction -------------------------------------------
    #: Writes the thumbnail hook. Deliberately no API key: the Foundry provider
    #: authenticates with the job's managed identity, so there is no secret to
    #: store or rotate.
    art_director: str
    foundry_endpoint: str
    foundry_api_version: str
    foundry_deployment: str
    thumbnail_hook_variants: int

    # -- Publishing --------------------------------------------------------
    privacy_status: PrivacyStatus
    video_category_id: str
    video_tags: tuple[str, ...]

    # -- Distribution ------------------------------------------------------
    sinks: tuple[str, ...]
    email_recipients: tuple[str, ...]

    # -- Review UI ---------------------------------------------------------
    review_host: str
    review_port: int
    auth_mode: AuthMode
    csrf_secret: str

    # -- Job trigger (review app -> scheduled Job, over ARM) ----------------
    job_trigger_enabled: bool
    azure_subscription_id: str
    azure_resource_group: str
    azure_job_name: str

    # -- Runtime -----------------------------------------------------------
    data_dir: Path
    log_level: str
    log_format: str
    log_to_file: bool
    media_retention_days: int

    # -- Retry policy ------------------------------------------------------
    #: A stage that keeps failing is dead-lettered rather than retried forever.
    #: Unbounded retries are a self-inflicted DoS against YouTube, Anthropic and
    #: VirusTotal, and a cost-amplification path a hostile attachment can trigger.
    max_stage_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float

    # -- Observability (CCOL) ----------------------------------------------
    # An empty connection string is the whole degradation switch: no exporter is
    # created and no telemetry package is imported.
    otel_connection_string: str
    otel_enabled: bool
    service_name: str
    service_version: str
    environment_name: str
    #: Injected by Container Apps into every job replica. Logged so an approval in
    #: the review app can be joined to the job execution it triggered.
    job_execution_name: str

    # Media type allow-list. Anything not listed here is rejected at ingest --
    # an allow-list, never a block-list, because the block-list is unbounded.
    image_extensions: frozenset[str] = field(
        default_factory=lambda: frozenset({".jpg", ".jpeg", ".png", ".heic", ".webp"})
    )
    video_extensions: frozenset[str] = field(
        default_factory=lambda: frozenset({".mp4", ".mov"})
    )

    # Derived runtime directories -----------------------------------------
    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def counters_dir(self) -> Path:
        return self.data_dir / "counters"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    def ensure_dirs(self) -> None:
        for path in (
            self.jobs_dir,
            self.media_dir,
            self.out_dir,
            self.logs_dir,
            self.counters_dir,
            self.locks_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, env_file: Path | None = None, *, strict: bool = True) -> Settings:
        """Build Settings from the environment (with .env loaded if present).

        ``strict=False`` skips the cross-field validation so that ``ytshort
        doctor`` can still report on a half-configured install rather than
        refusing to start.
        """
        dotenv_path = env_file or (PROJECT_ROOT / ".env")
        if dotenv_path.exists():
            load_dotenv(dotenv_path, override=False)

        problems: list[str] = []

        privacy = _env("YTSHORT_PRIVACY_STATUS", "private").lower()
        if privacy not in ("private", "unlisted", "public"):
            problems.append(
                f"YTSHORT_PRIVACY_STATUS must be private|unlisted|public, got {privacy!r}"
            )
            privacy = "private"

        pii_policy = _env("YTSHORT_PII_POLICY", "warn").lower()
        if pii_policy not in ("warn", "block"):
            problems.append(f"YTSHORT_PII_POLICY must be warn|block, got {pii_policy!r}")
            pii_policy = "warn"

        sinks = tuple(_env_list("YTSHORT_SINKS") or ["file"])
        recipients = tuple(_env_list("YTSHORT_EMAIL_RECIPIENTS"))
        if "email" in sinks and not recipients:
            problems.append(
                "YTSHORT_SINKS enables 'email' but YTSHORT_EMAIL_RECIPIENTS is empty"
            )

        # The sender allow-list is the pipeline's front door. An empty list means
        # anyone who emails the watched address can put media into a queue that
        # ends in a public YouTube upload, so this is a hard failure rather than a
        # warning.
        senders = tuple(s.lower() for s in _env_list("YTSHORT_ALLOWED_SENDERS"))
        if not senders:
            problems.append(
                "YTSHORT_ALLOWED_SENDERS is empty. Set at least one sender address -- "
                "an empty allow-list means any stranger who emails the watched mailbox "
                "can queue media for publication."
            )

        credential_store = _env("YTSHORT_CREDENTIAL_STORE", "file").lower()
        if credential_store not in ("file", "keyvault"):
            problems.append(
                f"YTSHORT_CREDENTIAL_STORE must be file|keyvault, got {credential_store!r}"
            )
            credential_store = "file"

        key_vault_uri = _env("YTSHORT_KEY_VAULT_URI")
        if credential_store == "keyvault" and not key_vault_uri:
            problems.append(
                "YTSHORT_CREDENTIAL_STORE=keyvault requires YTSHORT_KEY_VAULT_URI"
            )

        auth_mode = _env("YTSHORT_AUTH_MODE", "none").lower()
        if auth_mode not in ("platform", "none"):
            problems.append(f"YTSHORT_AUTH_MODE must be platform|none, got {auth_mode!r}")
            auth_mode = "none"

        job_trigger = _env("YTSHORT_JOB_TRIGGER_ENABLED", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        subscription = _env("AZURE_SUBSCRIPTION_ID")
        resource_group = _env("AZURE_RESOURCE_GROUP")
        job_name = _env("YTSHORT_AZURE_JOB_NAME")
        if job_trigger and not (subscription and resource_group and job_name):
            problems.append(
                "YTSHORT_JOB_TRIGGER_ENABLED=true requires AZURE_SUBSCRIPTION_ID, "
                "AZURE_RESOURCE_GROUP and YTSHORT_AZURE_JOB_NAME"
            )

        settings = cls(
            google_client_secret_file=Path(
                _env("YTSHORT_GOOGLE_CLIENT_SECRET_FILE") or "client_secret.json"
            ),
            google_token_file=Path(_env("YTSHORT_GOOGLE_TOKEN_FILE") or "token.json"),
            credential_store=credential_store,  # type: ignore[arg-type]
            key_vault_uri=key_vault_uri.rstrip("/"),
            kv_secret_refresh_token=_env(
                "YTSHORT_KV_SECRET_REFRESH_TOKEN", "google-refresh-token"
            ),
            kv_secret_client_id=_env("YTSHORT_KV_SECRET_CLIENT_ID", "google-client-id"),
            kv_secret_client_secret=_env(
                "YTSHORT_KV_SECRET_CLIENT_SECRET", "google-client-secret"
            ),
            # No 'is:unread' -- we no longer hold gmail.modify, so we cannot mark
            # mail read. The date bound keeps the listing small and dedupe is done
            # against the job store.
            gmail_query=_env("YTSHORT_GMAIL_QUERY", "has:attachment newer_than:7d"),
            allowed_senders=senders,
            max_emails_per_day=_env_int("YTSHORT_MAX_EMAILS_PER_DAY", 10, problems),
            max_total_attachment_bytes=_env_int(
                "YTSHORT_MAX_TOTAL_ATTACHMENT_BYTES", 20 * 1024 * 1024, problems
            ),
            ffmpeg_path=_env("YTSHORT_FFMPEG_PATH"),
            ffprobe_path=_env("YTSHORT_FFPROBE_PATH"),
            audio_dir=_resolve(_env("YTSHORT_AUDIO_DIR"), "assets/audio"),
            audio_credit=_env("YTSHORT_AUDIO_CREDIT"),
            bumper_seconds=_env_float("YTSHORT_BUMPER_SECONDS", 1.5, problems),
            max_short_seconds=_env_int("YTSHORT_MAX_SHORT_SECONDS", 180, problems),
            background_audio_gain=_env_float("YTSHORT_BACKGROUND_AUDIO_GAIN", 0.35, problems),
            pii_policy=pii_policy,  # type: ignore[arg-type]
            malware_scanner=_env("YTSHORT_MALWARE_SCANNER", "defender").lower(),
            virustotal_api_key=_env("VIRUSTOTAL_API_KEY"),
            moderation_provider=_env("YTSHORT_MODERATION_PROVIDER", "none").lower(),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            art_director=_env("YTSHORT_ART_DIRECTOR", "none").lower(),
            # The resource base, e.g. https://my-foundry.openai.azure.com -- the
            # AzureOpenAI client appends the route and api-version itself.
            foundry_endpoint=_env("YTSHORT_FOUNDRY_ENDPOINT").rstrip("/"),
            foundry_api_version=_env("YTSHORT_FOUNDRY_API_VERSION", DEFAULT_API_VERSION),
            # The *deployment* name, which may differ from the model name. A
            # mismatch returns 404 on an otherwise valid endpoint.
            foundry_deployment=_env("YTSHORT_FOUNDRY_DEPLOYMENT", "gpt-4o-mini"),
            thumbnail_hook_variants=_env_int("YTSHORT_THUMBNAIL_HOOK_VARIANTS", 3, problems),
            privacy_status=privacy,  # type: ignore[arg-type]
            video_category_id=_env("YTSHORT_VIDEO_CATEGORY_ID", "22"),
            video_tags=tuple(_env_list("YTSHORT_VIDEO_TAGS") or ["shorts"]),
            sinks=sinks,
            email_recipients=recipients,
            review_host=_env("YTSHORT_REVIEW_HOST", "127.0.0.1"),
            review_port=_env_int("YTSHORT_REVIEW_PORT", 8080, problems),
            auth_mode=auth_mode,  # type: ignore[arg-type]
            csrf_secret=_env("YTSHORT_CSRF_SECRET"),
            job_trigger_enabled=job_trigger,
            azure_subscription_id=subscription,
            azure_resource_group=resource_group,
            azure_job_name=job_name,
            data_dir=_resolve(_env("YTSHORT_DATA_DIR"), "var"),
            log_level=_env("YTSHORT_LOG_LEVEL", "INFO").upper(),
            log_format=_env("YTSHORT_LOG_FORMAT", "console").lower(),
            log_to_file=_env("YTSHORT_LOG_TO_FILE", "true").lower()
            in ("1", "true", "yes"),
            media_retention_days=_env_int("YTSHORT_MEDIA_RETENTION_DAYS", 30, problems),
            max_stage_attempts=_env_int("YTSHORT_MAX_STAGE_ATTEMPTS", 5, problems),
            retry_base_seconds=_env_float("YTSHORT_RETRY_BASE_SECONDS", 30.0, problems),
            retry_max_seconds=_env_float("YTSHORT_RETRY_MAX_SECONDS", 3600.0, problems),
            # Azure's own conventional variable name, so the Bicep env var and every
            # Azure tool agree. Read here like everything else -- no module reads
            # os.environ directly.
            otel_connection_string=_env("APPLICATIONINSIGHTS_CONNECTION_STRING"),
            otel_enabled=_env("YTSHORT_TELEMETRY_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            service_name=_env("YTSHORT_SERVICE_NAME", "ytshort"),
            service_version=_env("YTSHORT_SERVICE_VERSION"),
            environment_name=_env("YTSHORT_ENVIRONMENT", "local"),
            job_execution_name=_env("CONTAINER_APP_JOB_EXECUTION_NAME"),
        )

        if strict and problems:
            raise ConfigError(
                "Invalid configuration:\n  - " + "\n  - ".join(problems)
            )
        return settings

    @property
    def uses_key_vault(self) -> bool:
        return self.credential_store == "keyvault"

    def validation_problems(self) -> list[str]:
        """Non-fatal readiness issues, used by ``ytshort doctor``."""
        problems: list[str] = []
        if not self.uses_key_vault and not self.google_client_secret_file.exists():
            problems.append(
                f"OAuth client secret not found at {self.google_client_secret_file}"
            )
        if not self.audio_dir.exists():
            problems.append(f"Audio directory not found at {self.audio_dir}")
        else:
            tracks = [
                p
                for p in self.audio_dir.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            ]
            if not tracks:
                problems.append(
                    f"No licensed audio track found in {self.audio_dir} "
                    "(drop an .mp3 you have the rights to use)"
                )
            problems.extend(self.audio_licence_problems())
        if "email" in self.sinks and not self.email_recipients:
            problems.append("email sink enabled but YTSHORT_EMAIL_RECIPIENTS is empty")
        if self.moderation_provider == "claude" and not self.anthropic_api_key:
            problems.append("moderation provider is 'claude' but ANTHROPIC_API_KEY is unset")
        if self.art_director == "foundry" and not self.foundry_endpoint:
            problems.append(
                "YTSHORT_ART_DIRECTOR=foundry but YTSHORT_FOUNDRY_ENDPOINT is unset"
            )
        if self.art_director == "foundry" and not _module_available("openai"):
            problems.append(
                "YTSHORT_ART_DIRECTOR=foundry but the openai package is not installed "
                "(uv sync --extra foundry)"
            )
        if self.telemetry_configured and not _module_available(
            "azure.monitor.opentelemetry"
        ):
            problems.append(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but the observability "
                "extra is not installed (uv sync --extra observability)"
            )
        return problems

    def audio_licence_problems(self) -> list[str]:
        """Tracks with no row in the licence manifest.

        docs/youtube-audit.md tells Google that AUDIO_LICENSES.md is authoritative
        for background-music licensing, and nothing used to check that a dropped
        file was actually recorded there. A hand-maintained table backing a claim
        made to an auditor is worth one cheap check.
        """
        if not self.audio_dir.exists():
            return []

        tracks = [
            p
            for p in self.audio_dir.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        ]
        manifest = self.audio_dir / "AUDIO_LICENSES.md"
        if not manifest.exists():
            return (
                [f"Audio licence manifest missing at {manifest}"] if tracks else []
            )

        # Only table rows count. Scanning the whole document would let any passing
        # mention satisfy the check -- including a warning that says to delete the
        # file -- and a control you can satisfy by naming the problem is no control.
        rows = "\n".join(
            line
            for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.lstrip().startswith("|")
        )
        return [
            f"{track.name} has no row in {manifest.name} -- record its source and "
            "licence, or a copyright claim cannot be disputed"
            for track in tracks
            if track.name not in rows
        ]

    @property
    def telemetry_configured(self) -> bool:
        """True when telemetry should export. Never logs the connection string."""
        return self.otel_enabled and bool(self.otel_connection_string)
