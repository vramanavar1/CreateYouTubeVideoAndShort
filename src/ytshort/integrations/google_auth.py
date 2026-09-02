"""OAuth for Gmail and YouTube, over a pluggable credential store.

Credentials come from a :mod:`~ytshort.integrations.credential_store` -- a local
``token.json`` in development, Key Vault in Azure. This module only knows how to
turn a stored credential into a usable one, and how to run the interactive
consent flow (which is local-only by nature: a container has no browser).

The gotcha this module exists to manage: while the Google Cloud consent screen is
in *Testing* mode, refresh tokens expire after 7 days and every run then dies
deep inside an API call with an opaque ``invalid_grant``. So credential health is
checked up front and reported in plain language, rather than surfacing at 2am as
a stack trace inside a stage.

Fix for that expiry: publish the OAuth app. It stays *unverified* -- you get a
"Google hasn't verified this app" interstitial, fine for a single-user pipeline --
but refresh tokens stop expiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ytshort.config import GOOGLE_SCOPES, Settings
from ytshort.integrations.credential_store import (
    CredentialError,
    CredentialStore,
    FileCredentialStore,
    build_credential_store,
)
from ytshort.observability.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


class AuthError(Exception):
    """Credentials are missing or unusable and cannot be repaired non-interactively."""


@dataclass
class CredentialStatus:
    ok: bool
    detail: str
    source: str = ""
    token_present: bool = False
    scopes_ok: bool = False
    expired: bool = False


def describe_credentials(settings: Settings) -> CredentialStatus:
    """Non-destructive health check used by ``ytshort doctor``.

    Never triggers a browser flow and never refreshes -- it only reports. For the
    Key Vault store this does reach the vault, which is the point: a doctor run
    should fail here rather than the first scheduled job failing at midnight.
    """
    store = build_credential_store(settings)

    if isinstance(store, FileCredentialStore):
        if not settings.google_client_secret_file.exists():
            return CredentialStatus(
                ok=False,
                source=store.describe(),
                detail=(
                    f"OAuth client secret missing at {settings.google_client_secret_file}. "
                    "Download a Desktop-app OAuth client from Google Cloud Console and "
                    "point YTSHORT_GOOGLE_CLIENT_SECRET_FILE at it."
                ),
            )
        if not store.token_path.exists():
            return CredentialStatus(
                ok=False,
                source=store.describe(),
                detail="No cached token. Run 'ytshort auth login' to grant access once.",
            )

    try:
        creds = store.load(GOOGLE_SCOPES)
    except CredentialError as exc:
        return CredentialStatus(ok=False, source=store.describe(), detail=str(exc))

    if creds is None:
        return CredentialStatus(
            ok=False,
            source=store.describe(),
            detail="No credential found. Run 'ytshort auth login'.",
        )

    granted = set(creds.scopes or [])
    missing = set(GOOGLE_SCOPES) - granted
    # The Key Vault store constructs the credential with exactly our scope list,
    # so a mismatch is only meaningful for the file store, where the cached token
    # records what was actually consented to.
    if missing and isinstance(store, FileCredentialStore):
        return CredentialStatus(
            ok=False,
            source=store.describe(),
            token_present=True,
            detail=(
                "Cached token is missing scopes: "
                + ", ".join(sorted(missing))
                + ". Re-run 'ytshort auth login --force' to re-consent."
            ),
        )

    if not creds.valid and not creds.refresh_token:
        return CredentialStatus(
            ok=False,
            source=store.describe(),
            token_present=True,
            scopes_ok=True,
            expired=True,
            detail="Token is expired with no refresh token. Re-run 'ytshort auth login --force'.",
        )

    return CredentialStatus(
        ok=True,
        source=store.describe(),
        token_present=True,
        scopes_ok=True,
        expired=not creds.valid,
        detail=f"Credentials available from {store.describe()} with all required scopes.",
    )


def load_credentials(
    settings: Settings,
    *,
    allow_interactive: bool = False,
    store: CredentialStore | None = None,
):
    """Return usable credentials, refreshing or prompting as permitted."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    store = store or build_credential_store(settings)

    try:
        creds = store.load(GOOGLE_SCOPES)
    except CredentialError as exc:
        raise AuthError(str(exc)) from exc

    if creds is not None and creds.valid:
        return creds

    if creds is not None and creds.refresh_token:
        try:
            creds.refresh(Request())
            store.save(creds)
            return creds
        except RefreshError as exc:
            # This is the 7-day testing-mode expiry, or a revoked grant.
            log.warning("refresh token rejected", extra={"error": str(exc)})
            if not allow_interactive:
                raise AuthError(
                    "Google refused the refresh token. Either the consent screen is "
                    "still in Testing mode (refresh tokens expire after 7 days -- "
                    "publish the app), or access was revoked. Re-run 'ytshort auth "
                    "login --force' locally and update the stored credential."
                ) from exc
            creds = None

    if not allow_interactive:
        raise AuthError("No usable Google credentials. Run 'ytshort auth login' first.")

    return run_consent_flow(settings, store=store)


def run_consent_flow(settings: Settings, *, store: CredentialStore | None = None):
    """Open a browser for the installed-app consent flow and cache the result.

    Local-only by nature -- a container has no browser. In Azure the credential is
    read from Key Vault, having been produced by running this locally once.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    store = store or build_credential_store(settings)
    client_secret = settings.google_client_secret_file
    if not client_secret.exists():
        raise AuthError(f"OAuth client secret not found at {client_secret}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), GOOGLE_SCOPES)
    # access_type=offline + prompt=consent is what actually returns a refresh
    # token on a repeat authorisation; without prompt=consent Google reuses the
    # prior grant and omits it.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Opening a browser to authorise ytshort ({url})",
        success_message="ytshort is authorised. You can close this tab.",
    )
    store.save(creds)
    return creds
