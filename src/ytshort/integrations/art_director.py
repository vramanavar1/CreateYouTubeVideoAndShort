"""Thumbnail art direction behind a swappable provider.

The thumbnail is *composited*, never generated: the picture is always the sender's
own screened attachment. What a model contributes is the words and a little layout
judgement -- a hook that earns a click instead of the raw email subject, which reads
as a caption.

Default is a no-op returning the subject unchanged, because the pipeline must run
without an AI provider configured. When ``YTSHORT_ART_DIRECTOR=foundry`` the
optional ``openai`` extra talks to an Azure Foundry deployment using a **managed
identity** -- there is no API key anywhere in this module, which is the whole reason
this provider was chosen.

Everything the model returns is untrusted text on its way into a published image.
``ThumbnailDirection.sanitised()`` is what stands between it and the renderer.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

#: This call sits inside a stage that holds the job lock, so neither of these is
#: left to the SDK's defaults -- the same reasoning as integrations/moderation.py.
_REQUEST_TIMEOUT_SECONDS = 60.0
_MAX_RETRIES = 2

#: Entra scope for Foundry *inference* (the data plane). Not
#: ``cognitiveservices.azure.com`` -- a token for one audience is rejected by the
#: other, and the failure looks like a permissions problem rather than a wrong scope.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

#: Azure requires an explicit API version. Overridable because a deployment may
#: only support certain versions, and the error when it does not is opaque.
DEFAULT_API_VERSION = "2024-10-21"

#: Longest edge, in pixels, of the image actually sent. Vision input is billed by
#: pixel count, so this is the difference between a few hundred tokens and tens of
#: thousands. A hook does not need a 12 megapixel photo to be written well.
MAX_IMAGE_EDGE = 768

#: A hook has to fit a thumbnail at a glance. Longer than this and the renderer
#: shrinks the font until nobody reads it on a phone.
MAX_HOOK_CHARS = 48

_HEX_COLOUR = re.compile(r"\A#[0-9A-Fa-f]{6}\Z")
_POSITIONS = ("top", "bottom")

_SYSTEM = (
    "You write thumbnail text for YouTube Shorts. You are given the image that will "
    "appear in the thumbnail and the subject line of the email it arrived in.\n"
    "Write three hooks of three to six words each, ordered from plainest to boldest. "
    "Bolder means punchier phrasing, NEVER a stronger claim: every hook must describe "
    "what is actually visible in the image. Do not invent events, places, people or "
    "outcomes. Misleading thumbnails get channels penalised.\n"
    "Also pick one word from the hooks to emphasise in colour, an accent colour as "
    "#RRGGBB that will contrast with the image, and whether the text belongs at the "
    "top or bottom so it does not cover the subject of the photo."
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hooks", "emphasis", "text_position", "accent_hex", "rationale"],
    "properties": {
        "hooks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Three hooks, plainest first, three to six words each.",
        },
        "emphasis": {
            "type": "string",
            "description": "One word appearing in the hooks, to be coloured.",
        },
        "text_position": {"type": "string", "enum": list(_POSITIONS)},
        "accent_hex": {"type": "string", "description": "Contrasting colour as #RRGGBB."},
        "rationale": {
            "type": "string",
            "description": "One short sentence explaining the choice, shown to the reviewer.",
        },
    },
}

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass
class ThumbnailDirection:
    """What the renderer needs, plus enough context for the reviewer to judge it."""

    hooks: list[str] = field(default_factory=list)
    emphasis: str = ""
    text_position: str = "top"
    accent_hex: str = "#FFFFFF"
    rationale: str = ""
    provider: str = "none"
    skipped: bool = False
    detail: str = ""

    def sanitised(self, fallback: str) -> ThumbnailDirection:
        """Return a copy safe to render.

        Model output reaches a published image, so it is treated exactly like an
        attacker-supplied email subject. ``sanitise_title`` (in stages/thumbnail.py)
        still runs at render time; this is the structural pass -- length, closed
        sets, and a colour that is a colour.
        """
        hooks = [h.strip()[:MAX_HOOK_CHARS] for h in self.hooks if h and h.strip()]

        accent = self.accent_hex.strip()
        if not _HEX_COLOUR.match(accent):
            accent = "#FFFFFF"

        position = self.text_position if self.text_position in _POSITIONS else "top"

        # An emphasis word the hooks never contain would colour nothing, or worse,
        # colour a substring of an unrelated word.
        emphasis = self.emphasis.strip()
        if emphasis and not any(emphasis.lower() in h.lower() for h in hooks):
            emphasis = ""

        return ThumbnailDirection(
            hooks=hooks or [fallback],
            emphasis=emphasis,
            text_position=position,
            accent_hex=accent,
            rationale=self.rationale.strip()[:200],
            provider=self.provider,
            skipped=self.skipped,
            detail=self.detail,
        )


class ArtDirector(Protocol):
    name: str

    def direct(self, image: Path, subject: str, body: str) -> ThumbnailDirection: ...


class NoopArtDirector:
    """No provider configured. The subject becomes the hook, as it always has."""

    name = "none"

    def direct(self, image: Path, subject: str, body: str) -> ThumbnailDirection:
        return ThumbnailDirection(
            hooks=[subject],
            provider=self.name,
            skipped=True,
            detail="no art director configured",
        )


class FoundryArtDirector:
    """Azure Foundry (Azure OpenAI), authenticated with a managed identity.

    No API key is created, stored or transmitted. ``DefaultAzureCredential`` picks up
    the user-assigned identity from ``AZURE_CLIENT_ID`` in Azure, and the developer's
    ``az login`` session locally, so the same code path is exercised in both places.
    """

    name = "foundry"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        variants: int = 3,
        api_version: str = DEFAULT_API_VERSION,
    ) -> None:
        self._endpoint = endpoint
        self._deployment = deployment
        self._variants = variants
        self._api_version = api_version

    def direct(self, image: Path, subject: str, body: str) -> ThumbnailDirection:
        suffix = image.suffix.lower()
        media_type = _MEDIA_TYPES.get(suffix)
        if media_type is None:
            return self._skip(subject, f"cannot send {suffix or '(no suffix)'} to the model")

        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import AzureOpenAI
        except ImportError:
            return self._skip(
                subject, "openai/azure-identity not installed (uv sync --extra foundry)"
            )

        try:
            payload = _encode(image, media_type)
        except Exception as exc:  # noqa: BLE001 - a bad image must not fail the job
            return self._skip(subject, f"could not read the image: {exc!r}")

        try:
            # AzureOpenAI, not OpenAI(base_url=..., api_key=<callable>). Microsoft's
            # sample shows the latter, but on this SDK a callable api_key is
            # silently discarded -- the client ends up with no Authorization header
            # at all and fails with 401 only once deployed. AzureOpenAI calls the
            # provider on every request, which is also what makes token refresh
            # work for a long-running process.
            client = AzureOpenAI(
                azure_endpoint=self._endpoint,
                api_version=self._api_version,
                azure_ad_token_provider=get_bearer_token_provider(
                    DefaultAzureCredential(), FOUNDRY_SCOPE
                ),
                timeout=_REQUEST_TIMEOUT_SECONDS,
                max_retries=_MAX_RETRIES,
            )
            response = client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": payload}},
                            {
                                "type": "text",
                                "text": (
                                    f"Email subject: {subject}\n"
                                    f"Body preview: {body[:400]}\n"
                                    f"Write {self._variants} hooks."
                                ),
                            },
                        ],
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "thumbnail_direction",
                        "strict": True,
                        "schema": _SCHEMA,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - never fail a job over a thumbnail hook
            # A 403 here is usually RBAC that has not propagated yet (Azure documents
            # up to five minutes); a 404 means the deployment name is wrong.
            log.warning("art director call failed", extra={"error": repr(exc)})
            return self._skip(subject, f"call failed: {exc!r}")

        return self._parse(response, subject)

    def _parse(self, response: object, subject: str) -> ThumbnailDirection:
        import json

        try:
            content = response.choices[0].message.content  # type: ignore[attr-defined]
            data = json.loads(content)
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return self._skip(subject, f"unparseable response: {exc!r}")

        return ThumbnailDirection(
            hooks=list(data.get("hooks") or []),
            emphasis=str(data.get("emphasis") or ""),
            text_position=str(data.get("text_position") or "top"),
            accent_hex=str(data.get("accent_hex") or "#FFFFFF"),
            rationale=str(data.get("rationale") or ""),
            provider=self.name,
        ).sanitised(subject)

    def _skip(self, subject: str, detail: str) -> ThumbnailDirection:
        return ThumbnailDirection(
            hooks=[subject], provider=self.name, skipped=True, detail=detail
        )


def _encode(image: Path, media_type: str) -> str:
    """Downscale, then base64 as a data URL.

    Vision input is billed by pixel count, so the resize is the single biggest cost
    lever in this feature -- a phone photo is tens of thousands of tokens untouched
    and a few hundred at 768 px. It costs nothing in hook quality: the model is
    reading composition and colour, not fine detail.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(image) as raw:
        source = raw.convert("RGB")
        source.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        buffer = BytesIO()
        source.save(buffer, format="JPEG", quality=85)

    encoded = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def build_art_director(
    provider: str,
    endpoint: str = "",
    deployment: str = "",
    variants: int = 3,
    api_version: str = DEFAULT_API_VERSION,
) -> ArtDirector:
    if provider == "foundry":
        if not endpoint:
            log.warning("art director is 'foundry' but no endpoint is set; disabling")
            return NoopArtDirector()
        return FoundryArtDirector(endpoint, deployment, variants, api_version)
    if provider not in ("", "none"):
        log.warning("unknown art director, disabling", extra={"provider": provider})
    return NoopArtDirector()
