from __future__ import annotations

import json
import os
import stat

import pytest

from codex_provider_switchboard.infrastructure.credential_store import (
    CredentialStore,
    CredentialStoreError,
)


def test_direct_credentials_are_atomic_private_and_never_returned(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "private" / "credentials.json"
    store = CredentialStore(path)

    store.set_api_key("openai", "sk-test-secret")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["credentials"]["openai"]["key"] == "sk-test-secret"
    safe = store.safe_view()
    assert safe["providers"]["openai"]["configured"] is True
    assert safe["providers"]["openai"]["source"] == "credential_file"
    assert "sk-test-secret" not in json.dumps(safe)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment")
    credential, source = store.resolve("openai")
    assert credential == {"type": "api_key", "key": "sk-environment"}
    assert source == "env:OPENAI_API_KEY"


def test_direct_credential_store_rejects_symlinks(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"credentials":{}}')
    link = tmp_path / "credentials.json"
    os.symlink(target, link)

    with pytest.raises(CredentialStoreError, match="symbolic links"):
        CredentialStore(link).read("openai")


def test_platform_auth_modes_prevent_misclassified_tokens(tmp_path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    with pytest.raises(ValueError, match="does not support API-key"):
        store.set_api_key("github_copilot", "github-token")
    with pytest.raises(ValueError, match="does not support OAuth"):
        store.set_oauth(
            "openai",
            access="access",
            refresh="refresh",
            expires_at=1,
        )
