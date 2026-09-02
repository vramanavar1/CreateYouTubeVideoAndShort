"""Start the scheduled Container Apps Job on demand, over ARM.

This is what lets the review app stay free of Google credentials. Approving does
not publish in the web request; it records the decision and asks the Job -- the
only workload that can read Key Vault and talk to Google -- to run now instead of
waiting for the next cron tick.

Authentication is the app's managed identity, whose only write permission is
``Microsoft.App/jobs/start/action`` scoped to this one Job. It cannot read the
vault, cannot touch other resources, and cannot even stop the job it starts.

A single POST, so ``urllib`` rather than another HTTP dependency.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ytshort.observability.logging import get_logger

if TYPE_CHECKING:
    from ytshort.config import Settings

log = get_logger(__name__)

_ARM = "https://management.azure.com"
_API_VERSION = "2024-03-01"
_SCOPE = "https://management.azure.com/.default"
_TIMEOUT_SECONDS = 30


@dataclass
class TriggerResult:
    ok: bool
    detail: str


class JobTrigger(Protocol):
    def start(self) -> TriggerResult: ...


class NoopJobTrigger:
    """Local development: there is no Azure Job, and the CLI does the work."""

    def start(self) -> TriggerResult:
        return TriggerResult(
            ok=False, detail="job trigger disabled; run 'ytshort run' to continue the job"
        )


class ContainerAppsJobTrigger:
    def __init__(self, subscription_id: str, resource_group: str, job_name: str) -> None:
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.job_name = job_name

    @property
    def url(self) -> str:
        return (
            f"{_ARM}/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.App/jobs/{self.job_name}"
            f"/start?api-version={_API_VERSION}"
        )

    def _token(self) -> str:
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential().get_token(_SCOPE).token

    def start(self) -> TriggerResult:
        try:
            token = self._token()
        except ImportError:
            return TriggerResult(
                ok=False, detail="azure-identity not installed (uv sync --extra azure)"
            )
        except Exception as exc:  # noqa: BLE001 - identity failures are environmental
            return TriggerResult(ok=False, detail=f"could not acquire an ARM token: {exc}")

        request = urllib.request.Request(  # noqa: S310 - fixed https ARM endpoint
            self.url,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Length": "0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
                name = ""
                with contextlib.suppress(json.JSONDecodeError, AttributeError):
                    name = json.loads(body).get("name", "")
                log.info("scheduled job triggered", extra={"execution": name})
                return TriggerResult(ok=True, detail=f"execution {name or 'started'}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            log.warning("job trigger rejected", extra={"status": exc.code})
            return TriggerResult(ok=False, detail=f"ARM returned {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            return TriggerResult(ok=False, detail=f"ARM unreachable: {exc.reason}")


def build_job_trigger(settings: Settings) -> JobTrigger:
    if not settings.job_trigger_enabled:
        return NoopJobTrigger()
    return ContainerAppsJobTrigger(
        settings.azure_subscription_id,
        settings.azure_resource_group,
        settings.azure_job_name,
    )
