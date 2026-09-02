"""Where the Google credential lives, and how it is loaded.

The key observation this module is built on: **only the refresh token is
long-lived**. The access token expires in an hour and can be minted fresh from
the refresh token on every run. So there is no need to persist a credential file
anywhere in the cloud deployment -- the refresh token sits in Key Vault, is read
into memory, and produces an access token that dies with the process.

Two implementations behind one seam:

* ``FileCredentialStore`` -- the ``token.json`` written by the local consent flow.
  Used for development. It *does* write back, because the local flow refreshes and
  re-consents interactively.
* ``KeyVaultCredentialStore`` -- reads three secrets from Key Vault and never
  writes anything. Rotating the credential is ``az keyvault secret set``, with no
  redeploy and nothing to clean off a disk.

The Key Vault path requires the optional ``azure`` extra; the import is lazy so a
local install stays light.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ytshort.observability.logging import get_logger

if TYPE_CHECKING:
    from ytshort.config import Settings

log = get_logger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


class CredentialError(Exception):
    """The credential could not be loaded from its configured home."""


class CredentialStore(Protocol):
    kind: str

    def load(self, scopes: list[str]):
        """Return google.oauth2.credentials.Credentials, or None if absent."""
        ...

    def save(self, credentials) -> None:
        """Persist a credential. May be a no-op for read-only stores."""
        ...

    def describe(self) -> str:
        """Human-readable location, for doctor output. Never the secret itself."""
        ...


class FileCredentialStore:
    """The local ``token.json`` written by ``ytshort auth login``."""

    kind = "file"

    def __init__(self, token_path: Path) -> None:
        self.token_path = Path(token_path)

    def load(self, scopes: list[str]):
        from google.oauth2.credentials import Credentials

        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self.token_path), scopes)
        except (ValueError, OSError) as exc:
            log.warning("cached token unreadable, discarding", extra={"error": str(exc)})
            return None

    def save(self, credentials) -> None:
        import contextlib

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        # Best-effort; Windows ACLs are not chmod.
        with contextlib.suppress(OSError):
            self.token_path.chmod(0o600)
        log.info("credentials cached", extra={"path": str(self.token_path)})

    def describe(self) -> str:
        return f"file {self.token_path}"


class KeyVaultCredentialStore:
    """Refresh token in Key Vault; the access token never leaves memory.

    Deliberately write-only-never: ``save`` is a no-op. If google-auth refreshes
    the access token there is nothing to persist, and if Google ever rotates the
    refresh token the correct response is a loud failure telling the operator to
    re-consent, not a silent write-back that would need Key Vault write access.
    """

    kind = "keyvault"

    def __init__(
        self,
        vault_uri: str,
        refresh_token_secret: str,
        client_id_secret: str,
        client_secret_secret: str,
    ) -> None:
        self.vault_uri = vault_uri
        self._names = {
            "refresh_token": refresh_token_secret,
            "client_id": client_id_secret,
            "client_secret": client_secret_secret,
        }

    def _client(self):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise CredentialError(
                "Key Vault credential store needs the azure extra: "
                "uv sync --extra azure"
            ) from exc

        return SecretClient(
            vault_url=self.vault_uri, credential=DefaultAzureCredential()
        )

    def load(self, scopes: list[str]):
        from google.oauth2.credentials import Credentials

        client = self._client()
        try:
            values = {
                field: client.get_secret(name).value
                for field, name in self._names.items()
            }
        except Exception as exc:  # noqa: BLE001 - surface the vault error verbatim
            raise CredentialError(
                f"could not read Google credential secrets from {self.vault_uri}: {exc}"
            ) from exc

        missing = [field for field, value in values.items() if not value]
        if missing:
            raise CredentialError(
                f"Key Vault secrets are empty: {', '.join(self._names[m] for m in missing)}"
            )

        # token=None forces a refresh on first use, which is what we want: the
        # access token is minted fresh for this process and expires with it.
        return Credentials(
            token=None,
            refresh_token=values["refresh_token"].strip(),
            client_id=values["client_id"].strip(),
            client_secret=values["client_secret"].strip(),
            token_uri=_TOKEN_URI,
            scopes=scopes,
        )

    def save(self, credentials) -> None:
        # Intentionally nothing. See the class docstring.
        return None

    def describe(self) -> str:
        return f"key vault {self.vault_uri}"


def build_credential_store(settings: Settings) -> CredentialStore:
    if settings.credential_store == "keyvault":
        return KeyVaultCredentialStore(
            settings.key_vault_uri,
            settings.kv_secret_refresh_token,
            settings.kv_secret_client_id,
            settings.kv_secret_client_secret,
        )
    return FileCredentialStore(settings.google_token_file)


def extract_for_key_vault(token_path: Path) -> dict[str, str]:
    """Pull the three values that go into Key Vault out of a local token.json.

    Used by ``ytshort auth export`` so nobody has to hand-parse the file and
    accidentally paste a trailing newline into a secret.
    """
    data = json.loads(Path(token_path).read_text(encoding="utf-8"))
    missing = [k for k in ("refresh_token", "client_id", "client_secret") if not data.get(k)]
    if missing:
        raise CredentialError(
            f"{token_path} has no {', '.join(missing)} -- re-run 'ytshort auth login "
            "--force' so a refresh token is issued."
        )
    return {
        "refresh_token": data["refresh_token"],
        "client_id": data["client_id"],
        "client_secret": data["client_secret"],
    }
