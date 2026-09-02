"""Credential storage — the file store, the Key Vault store, and scope policy."""

from __future__ import annotations

import json

import pytest

from ytshort.config import GOOGLE_SCOPES, ConfigError, Settings
from ytshort.integrations.credential_store import (
    CredentialError,
    FileCredentialStore,
    KeyVaultCredentialStore,
    build_credential_store,
    extract_for_key_vault,
)

_TOKEN = {
    "token": "access-token",
    "refresh_token": "refresh-token-value",
    "client_id": "client-id-value",
    "client_secret": "client-secret-value",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": GOOGLE_SCOPES,
}


class TestScopePolicy:
    def test_gmail_modify_is_not_requested(self) -> None:
        # Write access to the whole mailbox, for a credential that lives in the
        # cloud, to do something the job store already does.
        assert not any("gmail.modify" in scope for scope in GOOGLE_SCOPES)

    def test_force_ssl_is_requested_up_front(self) -> None:
        # Needed post-audit for videos.update; asking now avoids a re-consent.
        assert any("youtube.force-ssl" in scope for scope in GOOGLE_SCOPES)

    def test_scopes_are_read_and_send_only_on_gmail(self) -> None:
        gmail = [s for s in GOOGLE_SCOPES if "gmail" in s]
        assert sorted(gmail) == [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]


class TestFileStore:
    def test_round_trips_a_credential(self, settings, tmp_path) -> None:
        from google.oauth2.credentials import Credentials

        path = tmp_path / "token.json"
        path.write_text(json.dumps(_TOKEN), encoding="utf-8")
        store = FileCredentialStore(path)

        creds = store.load(GOOGLE_SCOPES)

        assert isinstance(creds, Credentials)
        assert creds.refresh_token == "refresh-token-value"

    def test_absent_file_is_none_not_an_error(self, tmp_path) -> None:
        assert FileCredentialStore(tmp_path / "nope.json").load(GOOGLE_SCOPES) is None

    def test_corrupt_file_is_none_not_an_error(self, tmp_path) -> None:
        path = tmp_path / "token.json"
        path.write_text("{ not json", encoding="utf-8")

        assert FileCredentialStore(path).load(GOOGLE_SCOPES) is None


class TestKeyVaultStore:
    class _FakeSecret:
        def __init__(self, value: str) -> None:
            self.value = value

    class _FakeClient:
        def __init__(self, values: dict[str, str]) -> None:
            self.values = values
            self.reads: list[str] = []

        def get_secret(self, name: str):
            self.reads.append(name)
            if name not in self.values:
                raise KeyError(name)
            return TestKeyVaultStore._FakeSecret(self.values[name])

    def _store(self, values: dict[str, str]) -> KeyVaultCredentialStore:
        store = KeyVaultCredentialStore(
            "https://kv.vault.azure.net",
            "google-refresh-token",
            "google-client-id",
            "google-client-secret",
        )
        store._client = lambda: TestKeyVaultStore._FakeClient(values)  # type: ignore[method-assign]
        return store

    def _values(self) -> dict[str, str]:
        return {
            "google-refresh-token": "refresh-token-value",
            "google-client-id": "client-id-value",
            "google-client-secret": "client-secret-value",
        }

    def test_builds_a_credential_with_no_access_token(self) -> None:
        creds = self._store(self._values()).load(GOOGLE_SCOPES)

        # token=None forces a refresh, so the access token is minted for this
        # process and dies with it -- nothing long-lived is held.
        assert creds.token is None
        assert creds.refresh_token == "refresh-token-value"
        assert creds.client_id == "client-id-value"
        assert list(creds.scopes) == GOOGLE_SCOPES

    def test_trailing_whitespace_is_stripped(self) -> None:
        # The classic 'az keyvault secret set' paste error.
        values = {k: f"{v}\n" for k, v in self._values().items()}
        creds = self._store(values).load(GOOGLE_SCOPES)

        assert creds.refresh_token == "refresh-token-value"

    def test_save_is_a_no_op(self) -> None:
        store = self._store(self._values())
        creds = store.load(GOOGLE_SCOPES)

        assert store.save(creds) is None  # never writes back to the vault

    def test_an_empty_secret_is_a_clear_error(self) -> None:
        values = self._values() | {"google-refresh-token": ""}

        with pytest.raises(CredentialError, match="empty"):
            self._store(values).load(GOOGLE_SCOPES)

    def test_a_vault_failure_names_the_vault(self) -> None:
        with pytest.raises(CredentialError, match="kv.vault.azure.net"):
            self._store({}).load(GOOGLE_SCOPES)

    def test_describe_never_leaks_the_secret(self) -> None:
        described = self._store(self._values()).describe()

        assert "refresh-token-value" not in described
        assert "kv.vault.azure.net" in described


class TestSelection:
    def test_defaults_to_the_file_store(self, settings) -> None:
        assert isinstance(build_credential_store(settings), FileCredentialStore)

    def test_keyvault_selected_by_config(self, settings, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("YTSHORT_CREDENTIAL_STORE", "keyvault")
        monkeypatch.setenv("YTSHORT_KEY_VAULT_URI", "https://kv.vault.azure.net/")
        loaded = Settings.load(env_file=tmp_path / "absent.env")

        store = build_credential_store(loaded)

        assert isinstance(store, KeyVaultCredentialStore)
        assert store.vault_uri == "https://kv.vault.azure.net"  # trailing slash trimmed

    def test_keyvault_without_a_uri_is_refused(self, settings, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("YTSHORT_CREDENTIAL_STORE", "keyvault")

        with pytest.raises(ConfigError, match="YTSHORT_KEY_VAULT_URI"):
            Settings.load(env_file=tmp_path / "absent.env")


class TestExportForKeyVault:
    def test_extracts_the_three_values(self, tmp_path) -> None:
        path = tmp_path / "token.json"
        path.write_text(json.dumps(_TOKEN), encoding="utf-8")

        values = extract_for_key_vault(path)

        assert values == {
            "refresh_token": "refresh-token-value",
            "client_id": "client-id-value",
            "client_secret": "client-secret-value",
        }
        # The access token is deliberately not exported -- it would be stale
        # within the hour and is not what the vault is for.
        assert "token" not in values

    def test_a_token_without_a_refresh_token_is_rejected(self, tmp_path) -> None:
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"token": "x", "client_id": "y"}), encoding="utf-8")

        with pytest.raises(CredentialError, match="refresh_token"):
            extract_for_key_vault(path)
