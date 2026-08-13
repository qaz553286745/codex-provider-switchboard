from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest

from codex_provider_switchboard.infrastructure import oauth as oauth_module
from codex_provider_switchboard.infrastructure.credential_store import CredentialStore
from codex_provider_switchboard.infrastructure.oauth import (
    OAuthError,
    OAuthLoginManager,
)
from codex_provider_switchboard.settings import AppSettings


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        host="127.0.0.1",
        port=8787,
        token=None,
        max_request_bytes=1_048_576,
        debug_requests=False,
        session_reuse=True,
        session_ttl_seconds=3600,
        kiro_cli="kiro-cli",
        kiro_model="gpt-5.6-sol",
        kiro_workdir=tmp_path / "kiro",
        kiro_timeout_seconds=30,
        kiro_max_concurrency=1,
        kiro_max_prompt_bytes=1_048_576,
        kiro_context_recovery_prompt_bytes=512 * 1_024,
        kiro_max_output_bytes=1_048_576,
        kiro_allow_requested_model=False,
        kiro_tool_batching=True,
        cursor_cli="cursor-agent",
        cursor_workdir=tmp_path / "cursor",
        cursor_max_concurrency=1,
        cursor_max_prompt_bytes=1_048_576,
        cursor_max_output_bytes=1_048_576,
        direct_oauth_timeout_seconds=5,
    )


def _jwt(account_id: str) -> str:
    def encoded(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    header = encoded({"alg": "none"})
    payload = encoded(
        {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    )
    return f"{header}.{payload}.x"


def test_chatgpt_device_login_handles_empty_pending_response_and_stores_privately(
    monkeypatch, tmp_path
) -> None:
    polls = 0
    access = _jwt("acct-test")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "device-1",
                    "user_code": "ABCD-EFGH",
                    "interval": 1,
                },
            )
        if request.url.path.endswith("/deviceauth/token"):
            polls += 1
            if polls == 1:
                return httpx.Response(404, content=b"")
            return httpx.Response(
                200,
                json={"authorization_code": "code-1", "code_verifier": "verify-1"},
            )
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": access,
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"Unexpected OAuth request: {request.url}")

    original_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(oauth_module.asyncio, "sleep", fast_sleep)
    credentials = CredentialStore(tmp_path / "credentials.json")
    manager = OAuthLoginManager(
        _settings(tmp_path), credentials, transport=httpx.MockTransport(handler)
    )

    async def scenario() -> dict:
        login = await manager.start(
            "openai_codex", {}, callback_base_url="http://127.0.0.1:8787"
        )
        task = manager._sessions[login["id"]].task
        assert task is not None
        await task
        return manager.status(login["id"])

    status = asyncio.run(scenario())
    assert status["status"] == "complete"
    stored = credentials.read("openai_codex")
    assert stored is not None
    assert stored["extra"]["account_id"] == "acct-test"
    assert "refresh-secret" not in json.dumps(credentials.safe_view())


def _write_pi_kiro_auth(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kiro": {
                    "type": "oauth",
                    "access": "access-token",
                    "refresh": "refresh-token|client-id|client-secret|idc",
                    "expires": 4_102_444_800_000,
                    "clientId": "client-id",
                    "clientSecret": "client-secret",
                    "region": "us-east-1",
                    "authMethod": "idc",
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_repeated_login_reuses_the_active_platform_session(tmp_path) -> None:
    credentials = CredentialStore(tmp_path / "credentials.json")
    manager = OAuthLoginManager(_settings(tmp_path), credentials)

    async def scenario() -> tuple[dict, dict, int]:
        started = asyncio.Event()

        async def waiting_flow(_session, _payload, _callback_base_url):
            started.set()
            await asyncio.Future()

        manager._login_flow = waiting_flow
        first = await manager.start(
            "kiro_direct", {}, callback_base_url="http://127.0.0.1:8787"
        )
        await started.wait()
        second = await manager.start(
            "kiro_direct", {}, callback_base_url="http://127.0.0.1:8787"
        )
        count = len(manager._sessions)
        await manager.close()
        return first, second, count

    first, second, count = asyncio.run(scenario())
    assert second["id"] == first["id"]
    assert count == 1


def test_resolve_accepts_a_freshly_refreshed_expired_credential(tmp_path) -> None:
    fresh_access = "fresh-access"
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_oauth(
        "anthropic",
        access="expired-access",
        refresh="refresh-token",
        expires_at=1,
        extra={},
    )
    manager = OAuthLoginManager(_settings(tmp_path), credentials)

    async def refresh(_platform_id, _credential):
        return {
            "access": fresh_access,
            "refresh": "fresh-refresh",
            "expires_at": int(time.time() * 1_000) + 3_600_000,
            "extra": {"scope": "test"},
        }

    manager._refresh = refresh

    resolved = asyncio.run(manager.resolve("anthropic"))

    assert resolved.token == fresh_access
    assert resolved.extra == {"scope": "test"}
    stored = credentials.read("anthropic")
    assert stored is not None
    assert stored["access"] == fresh_access


def test_import_pi_kiro_splits_packed_refresh_and_cancels_waiting_login(
    tmp_path,
) -> None:
    source = tmp_path / "pi" / "auth.json"
    _write_pi_kiro_auth(source)
    credentials = CredentialStore(tmp_path / "credentials.json")
    manager = OAuthLoginManager(_settings(tmp_path), credentials, pi_auth_path=source)

    async def scenario() -> dict:
        started = asyncio.Event()

        async def waiting_flow(_session, _payload, _callback_base_url):
            started.set()
            await asyncio.Future()

        manager._login_flow = waiting_flow
        login = await manager.start(
            "kiro_direct", {}, callback_base_url="http://127.0.0.1:8787"
        )
        await started.wait()
        await manager.import_credentials("kiro_direct", "pi")
        return manager.status(login["id"])

    login_status = asyncio.run(scenario())
    assert login_status["status"] == "cancelled"
    stored = credentials.read("kiro_direct")
    assert stored is not None
    assert stored["access"] == "access-token"
    assert stored["refresh"] == "refresh-token"
    assert stored["expires_at"] == 4_102_444_800_000
    assert stored["extra"] == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "region": "us-east-1",
        "auth_method": "idc",
        "subscription": True,
    }


def test_import_pi_kiro_rejects_arbitrary_sources_and_unsafe_files(tmp_path) -> None:
    credentials = CredentialStore(tmp_path / "credentials.json")

    source = tmp_path / "missing-fields.json"
    source.write_text(
        json.dumps({"kiro": {"type": "oauth", "access": "access-token"}}),
        encoding="utf-8",
    )
    source.chmod(0o600)
    manager = OAuthLoginManager(_settings(tmp_path), credentials, pi_auth_path=source)
    with pytest.raises(ValueError, match="source"):
        asyncio.run(manager.import_credentials("kiro_direct", "/tmp/auth.json"))
    with pytest.raises(OAuthError, match="incomplete"):
        asyncio.run(manager.import_credentials("kiro_direct", "pi"))

    source.write_text(
        json.dumps(
            {
                "kiro": {
                    "type": "oauth",
                    "access": "access-token",
                    "refresh": "refresh-token",
                    "expires": 4_102_444_800_000,
                    "clientId": ["not", "a", "string"],
                    "clientSecret": "client-secret",
                    "region": "us-east-1",
                    "authMethod": "idc",
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OAuthError, match="incomplete"):
        asyncio.run(manager.import_credentials("kiro_direct", "pi"))

    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * (512 * 1_024 + 1), encoding="utf-8")
    oversized.chmod(0o600)
    oversized_manager = OAuthLoginManager(
        _settings(tmp_path), credentials, pi_auth_path=oversized
    )
    with pytest.raises(OAuthError, match="size limit"):
        asyncio.run(oversized_manager.import_credentials("kiro_direct", "pi"))

    target = tmp_path / "target.json"
    _write_pi_kiro_auth(target)
    link = tmp_path / "linked-auth.json"
    link.symlink_to(target)
    linked_manager = OAuthLoginManager(
        _settings(tmp_path), credentials, pi_auth_path=link
    )
    with pytest.raises(OAuthError, match="symbolic link"):
        asyncio.run(linked_manager.import_credentials("kiro_direct", "pi"))


def test_resolve_uses_fresh_expiry_after_refresh(tmp_path, monkeypatch) -> None:
    expected_access = "fresh-access"
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_oauth(
        "xai",
        access="expired-access",
        refresh="refresh-token",
        expires_at=1,
    )
    manager = OAuthLoginManager(_settings(tmp_path), credentials)

    async def refresh(_platform_id: str, _credential: dict) -> dict:
        return {
            "access": expected_access,
            "refresh": "fresh-refresh",
            "expires_at": oauth_module._now_ms() + 3_600_000,
            "extra": {},
        }

    monkeypatch.setattr(manager, "_refresh", refresh)

    resolved = asyncio.run(manager.resolve("xai"))

    assert resolved.token == expected_access
    stored = credentials.read("xai")
    assert stored is not None
    assert stored["access"] == "fresh-access"


def test_oauth_json_rejects_an_oversized_streamed_body(tmp_path) -> None:
    body = b"x" * (oauth_module._MAX_AUTH_BODY + 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    manager = OAuthLoginManager(
        _settings(tmp_path),
        CredentialStore(tmp_path / "credentials.json"),
        transport=httpx.MockTransport(handler),
    )

    async def scenario() -> None:
        await manager._request_json(
            "GET", "https://example.test/token", provider="Example"
        )

    with pytest.raises(OAuthError, match="byte limit"):
        asyncio.run(scenario())
