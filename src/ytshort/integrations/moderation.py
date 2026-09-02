"""Image moderation behind a swappable provider.

Default is a no-op: the pipeline has a human gate, so an unavailable moderator
must not block a run. When ``YTSHORT_MODERATION_PROVIDER=claude`` the optional
``anthropic`` extra is used to classify the still image before it is published.

Categories are deliberately short and publishing-oriented. The model returns them
through a *strict* tool schema rather than free-form JSON, so the result either
validates or the call fails loudly -- no half-parsed verdicts.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

#: A hung moderation call stalls the screening stage and holds the job lock, so
#: neither of these is left to the SDK's defaults.
_REQUEST_TIMEOUT_SECONDS = 120.0
_MAX_RETRIES = 2

#: What we ask the model to look for. Keep this list stable -- it is echoed into
#: findings and therefore into the reviewer's screen.
CATEGORIES = (
    "sexual_content",
    "graphic_violence",
    "hate_or_harassment",
    "self_harm",
    "illegal_goods",
    "personal_document",  # passports, IDs, bank statements held up to camera
    "minor_present",
)

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_MODERATION_TOOL = {
    "name": "report_moderation",
    "description": "Report whether the image is safe to publish publicly on YouTube.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "safe_to_publish": {
                "type": "boolean",
                "description": "True only if none of the categories apply.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": list(CATEGORIES)},
                "description": "Every category that applies. Empty when safe.",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence explaining the verdict for a human reviewer.",
            },
        },
        "required": ["safe_to_publish", "categories", "rationale"],
    },
}

_SYSTEM = (
    "You screen images that are about to be published as a public YouTube Short. "
    "Judge only what is visible in the image. Report every category that applies "
    "using the report_moderation tool. Be precise rather than cautious: a false "
    "positive wastes a human reviewer's time, a false negative publishes something "
    "harmful. Always call the tool."
)


@dataclass
class ModerationResult:
    flagged: bool
    provider: str
    categories: list[str] = field(default_factory=list)
    detail: str = ""
    skipped: bool = False


class ImageModerator(Protocol):
    name: str

    def moderate(self, path: Path) -> ModerationResult: ...


class NoopModerator:
    name = "none"

    def moderate(self, path: Path) -> ModerationResult:
        return ModerationResult(
            flagged=False,
            provider=self.name,
            detail="image moderation disabled by configuration",
            skipped=True,
        )


class ClaudeModerator:
    """Vision moderation via the Anthropic Messages API.

    Requires the optional extra: ``uv sync --extra moderation``.
    """

    name = "claude"

    def __init__(self, api_key: str, model: str = "claude-opus-5") -> None:
        self._api_key = api_key
        self._model = model

    def moderate(self, path: Path) -> ModerationResult:
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            return ModerationResult(
                flagged=False,
                provider=self.name,
                detail=f"unsupported image type for moderation: {path.suffix}",
                skipped=True,
            )

        try:
            import anthropic
        except ImportError:
            return ModerationResult(
                flagged=False,
                provider=self.name,
                detail="anthropic SDK not installed (uv sync --extra moderation)",
                skipped=True,
            )

        # Explicit rather than relying on SDK defaults: this call sits inside the
        # screening stage, so a hung request would stall the pipeline and hold the
        # job lock. Two retries covers a blip; beyond that the stage's own bounded
        # retry takes over, and a failure here is a warn finding, not a block.
        client = anthropic.Anthropic(
            api_key=self._api_key,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
        )
        encoded = base64.standard_b64encode(path.read_bytes()).decode("utf-8")

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=2000,
                system=_SYSTEM,
                # Classification is a simple task; low effort keeps it cheap and
                # fast without meaningfully changing the verdict.
                output_config={"effort": "low"},
                tools=[_MODERATION_TOOL],
                tool_choice={"type": "tool", "name": "report_moderation"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": encoded,
                                },
                            },
                            {
                                "type": "text",
                                "text": "Screen this image for public publication.",
                            },
                        ],
                    }
                ],
            )
        except anthropic.APIError as exc:
            # An unreachable moderator must not fail the job -- it degrades to
            # "not screened", which the review UI shows.
            log.warning("moderation call failed", extra={"error": str(exc)})
            return ModerationResult(
                flagged=False,
                provider=self.name,
                detail=f"moderation call failed ({exc}); image was not screened",
                skipped=True,
            )

        # A safety refusal is itself signal: the model declined to analyse the
        # image, so a human should definitely look at it.
        if response.stop_reason == "refusal":
            return ModerationResult(
                flagged=True,
                provider=self.name,
                detail="the moderation model declined to analyse this image",
            )

        for block in response.content:
            if block.type == "tool_use" and block.name == "report_moderation":
                data = block.input
                categories = list(data.get("categories", []))
                safe = bool(data.get("safe_to_publish", False))
                return ModerationResult(
                    flagged=not safe or bool(categories),
                    provider=self.name,
                    categories=categories,
                    detail=str(data.get("rationale", "")),
                )

        return ModerationResult(
            flagged=False,
            provider=self.name,
            detail="moderation model returned no verdict; image was not screened",
            skipped=True,
        )


def build_moderator(provider: str, api_key: str = "") -> ImageModerator:
    if provider == "claude":
        if not api_key:
            log.warning("moderation provider is 'claude' but no API key is set")
            return NoopModerator()
        return ClaudeModerator(api_key)
    if provider not in ("none", ""):
        log.warning(
            "unknown moderation provider, falling back to none",
            extra={"provider": provider},
        )
    return NoopModerator()
