from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .. import __version__
from ..settings import AppSettings
from .credential_store import CredentialStore, CredentialStoreError
from .direct_catalog import DIRECT_PLATFORM_IDS, direct_platform

_OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_OPENAI_AUTH_BASE = "https://auth.openai.com"
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_CALLBACK_PORT = 53692
_XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
_GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_MAX_AUTH_BODY = 1 * 1_048_576
_REFRESH_SKEW_MS = 5 * 60 * 1_000
_LOGIN_RETENTION_SECONDS = 30 * 60
_SAFE_MESSAGE = re.compile(r"[^\x20-\x7e\u0080-\uffff]")

_KIRO_SCOPES = (
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
)
_KIRO_REGIONS = (
    "us-east-1",
    "eu-west-1",
    "eu-central-1",
    "us-east-2",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "ap-southeast-1",
    "ap-northeast-1",
    "us-west-2",
)


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    platform_id: str
    credential_type: str
    token: str
    source: str
    extra: dict[str, Any]


@dataclass(slots=True)
class _LoginSession:
    id: str
    platform_id: str
    created_at: float
    status: str = "starting"
    event: dict[str, Any] | None = None
    prompt: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None
    prompt_future: asyncio.Future[str] | None = None
    callback_future: asyncio.Future[dict[str, str]] | None = None

    def safe_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform_id": self.platform_id,
            "status": self.status,
            "event": self.event,
            "prompt": self.prompt,
            "error": self.error,
        }


def _safe_error(error: BaseException) -> str:
    if isinstance(error, OAuthError):
        value = str(error)
    elif isinstance(error, (asyncio.CancelledError, TimeoutError)):
        value = "Login was cancelled or timed out."
    else:
        value = f"Authentication failed ({type(error).__name__})."
    value = _SAFE_MESSAGE.sub("", value).strip()
    return value[:600] or "Authentication failed."


def _now_ms() -> int:
    return int(time.time() * 1_000)


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise OAuthError("OAuth server returned an invalid access token.")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OAuthError("OAuth server returned an invalid access token.") from exc
    if not isinstance(value, dict):
        raise OAuthError("OAuth server returned an invalid access token.")
    return value


def _required_string(value: dict[str, Any], field_name: str) -> str:
    result = value.get(field_name)
    if not isinstance(result, str) or not result:
        raise OAuthError(f"OAuth response did not include {field_name}.")
    return result


def _positive_number(
    value: dict[str, Any], field_name: str, *, fallback: float | None = None
) -> float:
    result = value.get(field_name, fallback)
    if not isinstance(result, (int, float)) or isinstance(result, bool) or result <= 0:
        raise OAuthError(f"OAuth response did not include a valid {field_name}.")
    return float(result)


def _trusted_https_url(
    value: str,
    *,
    hosts: set[str] | None = None,
    host_suffixes: set[str] | None = None,
) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    suffix_allowed = bool(
        host_suffixes
        and any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in host_suffixes
        )
    )
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or (
            (hosts is not None or host_suffixes is not None)
            and hostname not in (hosts or set())
            and not suffix_allowed
        )
    ):
        raise OAuthError("OAuth server returned an untrusted verification URL.")
    return value


class OAuthLoginManager:
    """Switchboard-owned OAuth coordinator and locked token refresher."""

    def __init__(
        self,
        settings: AppSettings,
        credentials: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.transport = transport
        self._sessions: dict[str, _LoginSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._refresh_locks = {
            platform_id: asyncio.Lock() for platform_id in DIRECT_PLATFORM_IDS
        }

    def _client(self, *, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 20)),
            follow_redirects=False,
            transport=self.transport,
            headers={"User-Agent": f"codex-provider-switchboard/{__version__}"},
        )

    @staticmethod
    async def _json(response: httpx.Response) -> dict[str, Any]:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > _MAX_AUTH_BODY:
                raise OAuthError("OAuth response exceeded the byte limit.")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OAuthError(
                f"OAuth server returned invalid JSON (HTTP {response.status_code})."
            ) from exc
        if not isinstance(value, dict):
            raise OAuthError("OAuth server returned an unexpected response.")
        return value

    @staticmethod
    def _oauth_http_error(provider: str, response: httpx.Response) -> OAuthError:
        return OAuthError(
            f"{provider} authentication returned HTTP {response.status_code}."
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        provider: str,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        try:
            async with (
                self._client(timeout=timeout) as client,
                client.stream(
                    method,
                    url,
                    json=json_body,
                    data=form_body,
                    headers=headers,
                ) as response,
            ):
                value = await self._json(response)
                if response.status_code >= 400:
                    raise self._oauth_http_error(provider, response)
        except httpx.HTTPError as exc:
            raise OAuthError(
                f"Could not reach {provider} authentication service."
            ) from exc
        return value

    async def _request_json_status(
        self,
        method: str,
        url: str,
        *,
        provider: str,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> tuple[int, dict[str, Any]]:
        try:
            async with (
                self._client(timeout=timeout) as client,
                client.stream(
                    method,
                    url,
                    json=json_body,
                    data=form_body,
                    headers=headers,
                ) as response,
            ):
                status_code = response.status_code
                try:
                    value = await self._json(response)
                except OAuthError:
                    if status_code >= 400:
                        return status_code, {}
                    raise
        except httpx.HTTPError as exc:
            raise OAuthError(
                f"Could not reach {provider} authentication service."
            ) from exc
        return status_code, value

    async def _notify(self, session: _LoginSession, event: dict[str, Any]) -> None:
        safe = dict(event)
        for key in ("message", "instructions"):
            if key in safe and isinstance(safe[key], str):
                safe[key] = safe[key][:1_000]
        session.event = safe
        if safe.get("type") in {"device_code", "auth_url"}:
            session.status = "waiting"

    async def _prompt(
        self,
        session: _LoginSession,
        *,
        prompt_type: str,
        message: str,
        placeholder: str = "",
    ) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        session.prompt_future = future
        session.prompt = {
            "type": prompt_type,
            "message": message[:1_000],
            "placeholder": placeholder[:500],
        }
        session.status = "waiting"
        try:
            return await future
        finally:
            session.prompt = None
            session.prompt_future = None

    def _cleanup_sessions(self) -> None:
        cutoff = time.monotonic() - _LOGIN_RETENTION_SECONDS
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if session.created_at < cutoff
            and session.status in {"complete", "failed", "cancelled"}
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)

    async def start(
        self,
        platform_id: str,
        payload: dict[str, Any],
        *,
        callback_base_url: str,
    ) -> dict[str, Any]:
        platform = direct_platform(platform_id)
        if "oauth" not in platform.auth_modes:
            raise ValueError(f"{platform.name} does not support OAuth login.")
        if len(json.dumps(payload, ensure_ascii=False)) > 8_192:
            raise ValueError("OAuth options are too large.")
        async with self._sessions_lock:
            self._cleanup_sessions()
            existing = next(
                (
                    session
                    for session in self._sessions.values()
                    if session.platform_id == platform_id
                    and session.status in {"starting", "waiting"}
                ),
                None,
            )
            if existing is not None:
                return existing.safe_view()
            active = sum(
                session.status in {"starting", "waiting"}
                for session in self._sessions.values()
            )
            if active >= 8:
                raise OAuthError("Too many authentication operations are active.")
            session_id = secrets.token_urlsafe(24)
            session = _LoginSession(
                id=session_id,
                platform_id=platform_id,
                created_at=time.monotonic(),
            )
            self._sessions[session_id] = session
            session.task = asyncio.create_task(
                self._run_login(session, payload, callback_base_url),
                name=f"switchboard-oauth-{platform_id}",
            )
        return session.safe_view()

    async def _run_login(
        self,
        session: _LoginSession,
        payload: dict[str, Any],
        callback_base_url: str,
    ) -> None:
        try:
            credential = await asyncio.wait_for(
                self._login_flow(session, payload, callback_base_url),
                timeout=self.settings.direct_oauth_timeout_seconds,
            )
            self.credentials.set_oauth(session.platform_id, **credential)
            session.event = {
                "type": "complete",
                "message": f"Signed in to {direct_platform(session.platform_id).name}.",
            }
            session.status = "complete"
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.error = "Login was cancelled."
        except (OAuthError, CredentialStoreError, ValueError, httpx.HTTPError) as exc:
            session.status = "failed"
            session.error = _safe_error(exc)
        except Exception as exc:  # defensive: never expose upstream response bodies
            session.status = "failed"
            session.error = _safe_error(exc)

    async def _login_flow(
        self,
        session: _LoginSession,
        payload: dict[str, Any],
        callback_base_url: str,
    ) -> dict[str, Any]:
        match session.platform_id:
            case "openai_codex":
                return await self._login_openai_codex(session)
            case "anthropic":
                return await self._login_anthropic(session)
            case "github_copilot":
                return await self._login_github_copilot(session, payload)
            case "xai":
                return await self._login_xai(session)
            case "openrouter":
                return await self._login_openrouter(
                    session, callback_base_url=callback_base_url
                )
            case "kiro_direct":
                return await self._login_kiro(session, payload)
            case _:
                raise OAuthError("Unsupported OAuth provider.")

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session.safe_view()

    def respond(self, session_id: str, value: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        future = session.prompt_future
        if future is None or future.done():
            raise ValueError("This login is not waiting for input.")
        if not isinstance(value, str) or len(value) > 16_384:
            raise ValueError("OAuth input is invalid or too long.")
        future.set_result(value)
        session.status = "starting"
        return session.safe_view()

    def receive_callback(
        self, session_id: str, parameters: dict[str, str]
    ) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        future = session.callback_future
        if future is None or future.done():
            raise ValueError("This OAuth callback is no longer active.")
        future.set_result(dict(parameters))
        session.status = "starting"
        return session.safe_view()

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.task is not None and not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        return session.safe_view()

    async def cancel_platform_logins(self, platform_id: str) -> None:
        if platform_id not in DIRECT_PLATFORM_IDS:
            raise ValueError(f"Unknown direct platform: {platform_id}")
        async with self._sessions_lock:
            tasks = [
                session.task
                for session in self._sessions.values()
                if session.platform_id == platform_id
                and session.status in {"starting", "waiting"}
                and session.task is not None
                and not session.task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        tasks = [
            session.task
            for session in self._sessions.values()
            if session.task is not None and not session.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def set_api_key(self, platform_id: str, api_key: str) -> None:
        self.credentials.set_api_key(platform_id, api_key)

    def logout(self, platform_id: str) -> bool:
        return self.credentials.delete(platform_id)

    async def resolve(self, platform_id: str) -> ResolvedCredential:
        async with self._refresh_locks[platform_id]:
            credential, source = self.credentials.resolve(platform_id)
            if credential is None:
                raise OAuthError(
                    f"{direct_platform(platform_id).name} is not authenticated."
                )
            credential_type = credential.get("type")
            if credential_type == "api_key":
                token = credential.get("key")
                if not isinstance(token, str) or not token:
                    raise OAuthError("Stored API key is invalid.")
                return ResolvedCredential(platform_id, "api_key", token, source, {})
            if credential_type != "oauth":
                raise OAuthError("Stored credential has an unsupported type.")

            expires_at = credential.get("expires_at")
            if (
                isinstance(expires_at, int)
                and expires_at <= _now_ms() + 60_000
                and credential.get("refresh")
            ):
                credential = await self._refresh(platform_id, credential)
                self.credentials.set_oauth(
                    platform_id,
                    access=str(credential["access"]),
                    refresh=str(credential.get("refresh") or ""),
                    expires_at=int(credential["expires_at"]),
                    extra=(
                        credential.get("extra")
                        if isinstance(credential.get("extra"), dict)
                        else {}
                    ),
                )
                expires_at = credential.get("expires_at")
            token = credential.get("access")
            if not isinstance(token, str) or not token:
                raise OAuthError("Stored OAuth credential is invalid.")
            if isinstance(expires_at, int) and expires_at <= _now_ms():
                raise OAuthError("Stored OAuth credential expired; sign in again.")
            extra = credential.get("extra")
            return ResolvedCredential(
                platform_id,
                "oauth",
                token,
                source,
                dict(extra) if isinstance(extra, dict) else {},
            )

    async def _refresh(
        self, platform_id: str, credential: dict[str, Any]
    ) -> dict[str, Any]:
        match platform_id:
            case "openai_codex":
                return await self._refresh_openai_codex(credential)
            case "anthropic":
                return await self._refresh_anthropic(credential)
            case "github_copilot":
                return await self._refresh_github_copilot(credential)
            case "xai":
                return await self._refresh_xai(credential)
            case "openrouter":
                return credential
            case "kiro_direct":
                return await self._refresh_kiro(credential)
            case _:
                raise OAuthError("This OAuth credential cannot be refreshed.")

    async def _login_openai_codex(self, session: _LoginSession) -> dict[str, Any]:
        device = await self._request_json(
            "POST",
            f"{_OPENAI_AUTH_BASE}/api/accounts/deviceauth/usercode",
            provider="OpenAI Codex",
            json_body={"client_id": _OPENAI_CLIENT_ID},
        )
        device_auth_id = _required_string(device, "device_auth_id")
        user_code = _required_string(device, "user_code")
        raw_interval = device.get("interval", 5)
        try:
            interval = max(1.0, float(raw_interval))
        except (TypeError, ValueError) as exc:
            raise OAuthError("OpenAI Codex returned an invalid poll interval.") from exc
        await self._notify(
            session,
            {
                "type": "device_code",
                "user_code": user_code,
                "verification_uri": f"{_OPENAI_AUTH_BASE}/codex/device",
                "interval_seconds": interval,
                "expires_in_seconds": 900,
            },
        )
        deadline = time.monotonic() + 900
        authorization_code = ""
        code_verifier = ""
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            status, value = await self._request_json_status(
                "POST",
                f"{_OPENAI_AUTH_BASE}/api/accounts/deviceauth/token",
                provider="OpenAI Codex",
                json_body={
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                },
            )
            if status < 400:
                authorization_code = _required_string(value, "authorization_code")
                code_verifier = _required_string(value, "code_verifier")
                break
            error = value.get("error")
            if isinstance(error, dict):
                error = error.get("code")
            if status in {403, 404} or error == "deviceauth_authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise OAuthError(
                f"OpenAI Codex device authorization failed (HTTP {status})."
            )
        if not authorization_code:
            raise OAuthError("OpenAI Codex device authorization timed out.")
        token = await self._request_json(
            "POST",
            f"{_OPENAI_AUTH_BASE}/oauth/token",
            provider="OpenAI Codex",
            form_body={
                "grant_type": "authorization_code",
                "client_id": _OPENAI_CLIENT_ID,
                "code": authorization_code,
                "code_verifier": code_verifier,
                "redirect_uri": f"{_OPENAI_AUTH_BASE}/deviceauth/callback",
            },
        )
        return self._openai_token_credential(token)

    @staticmethod
    def _openai_token_credential(
        token: dict[str, Any], *, previous_refresh: str = ""
    ) -> dict[str, Any]:
        access = _required_string(token, "access_token")
        refresh_value = token.get("refresh_token") or previous_refresh
        if not isinstance(refresh_value, str) or not refresh_value:
            raise OAuthError("OpenAI Codex returned no refresh token.")
        expires_in = _positive_number(token, "expires_in", fallback=3600)
        payload = _decode_jwt_payload(access)
        auth_claim = payload.get("https://api.openai.com/auth")
        account_id = (
            auth_claim.get("chatgpt_account_id")
            if isinstance(auth_claim, dict)
            else None
        )
        if not isinstance(account_id, str) or not account_id:
            raise OAuthError("OpenAI Codex token did not include an account ID.")
        return {
            "access": access,
            "refresh": refresh_value,
            "expires_at": _now_ms() + int(expires_in * 1_000) - _REFRESH_SKEW_MS,
            "extra": {"account_id": account_id, "subscription": True},
        }

    async def _refresh_openai_codex(self, credential: dict[str, Any]) -> dict[str, Any]:
        refresh = credential.get("refresh")
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("OpenAI Codex refresh token is missing; sign in again.")
        token = await self._request_json(
            "POST",
            f"{_OPENAI_AUTH_BASE}/oauth/token",
            provider="OpenAI Codex",
            form_body={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": _OPENAI_CLIENT_ID,
            },
        )
        return self._openai_token_credential(token, previous_refresh=refresh)

    async def _login_xai(self, session: _LoginSession) -> dict[str, Any]:
        status, device = await self._request_json_status(
            "POST",
            "https://auth.x.ai/oauth2/device/code",
            provider="xAI",
            form_body={
                "client_id": _XAI_CLIENT_ID,
                "scope": (
                    "openid profile email offline_access grok-cli:access api:access"
                ),
                "referrer": "codex-provider-switchboard",
            },
            headers={"Accept": "application/json"},
        )
        if status >= 400:
            raise OAuthError(f"xAI device authorization failed (HTTP {status}).")
        device_code = _required_string(device, "device_code")
        user_code = _required_string(device, "user_code")
        verification_uri = _trusted_https_url(
            str(
                device.get("verification_uri_complete")
                or _required_string(device, "verification_uri")
            ),
            hosts={"auth.x.ai"},
        )
        expires_in = _positive_number(device, "expires_in")
        interval = _positive_number(device, "interval", fallback=5)
        await self._notify(
            session,
            {
                "type": "device_code",
                "user_code": user_code,
                "verification_uri": verification_uri,
                "interval_seconds": interval,
                "expires_in_seconds": expires_in,
            },
        )
        await asyncio.sleep(interval)
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            status, token = await self._request_json_status(
                "POST",
                "https://auth.x.ai/oauth2/token",
                provider="xAI",
                form_body={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": _XAI_CLIENT_ID,
                    "device_code": device_code,
                },
                headers={"Accept": "application/json"},
            )
            if status < 400:
                return self._xai_token_credential(token)
            error = token.get("error")
            if error == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                await asyncio.sleep(interval)
                continue
            if error in {"access_denied", "authorization_denied"}:
                raise OAuthError("xAI device authorization was denied.")
            if error == "expired_token":
                raise OAuthError("xAI device authorization expired.")
            raise OAuthError(f"xAI token polling failed (HTTP {status}).")
        raise OAuthError("xAI device authorization timed out.")

    @staticmethod
    def _xai_token_credential(
        token: dict[str, Any], *, previous_refresh: str = ""
    ) -> dict[str, Any]:
        access = _required_string(token, "access_token")
        refresh = token.get("refresh_token") or previous_refresh
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("xAI returned no refresh token.")
        expires_in = _positive_number(token, "expires_in", fallback=3600)
        return {
            "access": access,
            "refresh": refresh,
            "expires_at": _now_ms() + int(expires_in * 1_000) - _REFRESH_SKEW_MS,
            "extra": {"subscription": True},
        }

    async def _refresh_xai(self, credential: dict[str, Any]) -> dict[str, Any]:
        refresh = credential.get("refresh")
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("xAI refresh token is missing; sign in again.")
        token = await self._request_json(
            "POST",
            "https://auth.x.ai/oauth2/token",
            provider="xAI",
            form_body={
                "grant_type": "refresh_token",
                "client_id": _XAI_CLIENT_ID,
                "refresh_token": refresh,
            },
            headers={"Accept": "application/json"},
        )
        return self._xai_token_credential(token, previous_refresh=refresh)

    async def _login_github_copilot(
        self, session: _LoginSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        unknown = set(payload) - {"enterprise_domain"}
        if unknown:
            raise ValueError(f"Unknown GitHub login option: {sorted(unknown)[0]}")
        enterprise = payload.get("enterprise_domain")
        if enterprise not in {None, ""}:
            raise OAuthError(
                "GitHub Enterprise login requires a separately registered OAuth app; "
                "this release supports github.com only."
            )
        device = await self._request_json(
            "POST",
            "https://github.com/login/device/code",
            provider="GitHub",
            form_body={
                "client_id": _GITHUB_COPILOT_CLIENT_ID,
                "scope": "read:user",
            },
            headers={"Accept": "application/json"},
        )
        device_code = _required_string(device, "device_code")
        user_code = _required_string(device, "user_code")
        verification_uri = _trusted_https_url(
            _required_string(device, "verification_uri"), hosts={"github.com"}
        )
        expires_in = _positive_number(device, "expires_in")
        interval = _positive_number(device, "interval", fallback=5)
        await self._notify(
            session,
            {
                "type": "device_code",
                "user_code": user_code,
                "verification_uri": verification_uri,
                "interval_seconds": interval,
                "expires_in_seconds": expires_in,
            },
        )
        deadline = time.monotonic() + expires_in
        await asyncio.sleep(interval)
        github_token = ""
        while time.monotonic() < deadline:
            status, token = await self._request_json_status(
                "POST",
                "https://github.com/login/oauth/access_token",
                provider="GitHub",
                form_body={
                    "client_id": _GITHUB_COPILOT_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            if status < 400 and isinstance(token.get("access_token"), str):
                github_token = str(token["access_token"])
                break
            error = token.get("error")
            if error == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            if error == "slow_down":
                interval = max(interval + 5, float(token.get("interval") or 0))
                await asyncio.sleep(interval)
                continue
            raise OAuthError("GitHub device authorization failed.")
        if not github_token:
            raise OAuthError("GitHub device authorization timed out.")
        return await self._copilot_credential(github_token)

    @staticmethod
    def _copilot_headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "GitHubCopilotChat/0.35.0",
            "Editor-Version": "vscode/1.107.0",
            "Editor-Plugin-Version": "copilot-chat/0.35.0",
            "Copilot-Integration-Id": "vscode-chat",
            "X-GitHub-Api-Version": "2026-06-01",
        }

    @staticmethod
    def _copilot_base_url(token: str) -> str:
        match = re.search(r"(?:^|;)proxy-ep=([^;]+)", token)
        host = match.group(1).strip().lower() if match else ""
        if host.startswith("proxy."):
            host = f"api.{host[6:]}"
        if not host.endswith(".githubcopilot.com"):
            return "https://api.individual.githubcopilot.com"
        return f"https://{host}"

    async def _copilot_credential(self, github_token: str) -> dict[str, Any]:
        value = await self._request_json(
            "GET",
            "https://api.github.com/copilot_internal/v2/token",
            provider="GitHub Copilot",
            headers=self._copilot_headers(github_token),
        )
        access = _required_string(value, "token")
        expires_at = int(_positive_number(value, "expires_at") * 1_000)
        base_url = self._copilot_base_url(access)
        available: list[str] = []
        try:
            catalog = await self._request_json(
                "GET",
                f"{base_url}/models",
                provider="GitHub Copilot",
                headers=self._copilot_headers(access),
                timeout=10,
            )
            raw_models = catalog.get("data")
            if isinstance(raw_models, list):
                for item in raw_models[:1_000]:
                    if not isinstance(item, dict) or not isinstance(
                        item.get("id"), str
                    ):
                        continue
                    capabilities = item.get("capabilities")
                    supports = (
                        capabilities.get("supports")
                        if isinstance(capabilities, dict)
                        else None
                    )
                    if (
                        isinstance(supports, dict)
                        and supports.get("tool_calls") is False
                    ):
                        continue
                    available.append(item["id"][:200])
        except OAuthError:
            available = []
        return {
            "access": access,
            "refresh": github_token,
            "expires_at": expires_at - _REFRESH_SKEW_MS,
            "extra": {
                "base_url": base_url,
                "available_model_ids": available,
                "subscription": True,
            },
        }

    async def _refresh_github_copilot(
        self, credential: dict[str, Any]
    ) -> dict[str, Any]:
        github_token = credential.get("refresh")
        if not isinstance(github_token, str) or not github_token:
            raise OAuthError("GitHub token is missing; sign in again.")
        return await self._copilot_credential(github_token)

    async def _login_openrouter(
        self,
        session: _LoginSession,
        *,
        callback_base_url: str,
    ) -> dict[str, Any]:
        parsed_base = urlsplit(callback_base_url)
        if (
            parsed_base.scheme != "http"
            or parsed_base.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_base.username
            or parsed_base.password
        ):
            raise OAuthError("OpenRouter OAuth callback must use a loopback URL.")
        verifier, challenge = _pkce()
        callback_url = (
            callback_base_url.rstrip("/")
            + f"/api/control/direct/oauth/callback/{session.id}"
        )
        authorize_url = "https://openrouter.ai/auth?" + urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        loop = asyncio.get_running_loop()
        session.callback_future = loop.create_future()
        await self._notify(
            session,
            {
                "type": "auth_url",
                "url": authorize_url,
                "instructions": "Complete OpenRouter sign-in in the browser.",
            },
        )
        prompt_task = asyncio.create_task(
            self._prompt(
                session,
                prompt_type="manual_code",
                message=(
                    "If the callback cannot reach this Mac, paste the final redirect "
                    "URL or authorization code."
                ),
                placeholder=callback_url,
            )
        )
        callback_future = session.callback_future
        done, pending = await asyncio.wait(
            {callback_future, prompt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for item in pending:
            item.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        session.callback_future = None
        if callback_future in done:
            parameters = callback_future.result()
            if parameters.get("error"):
                raise OAuthError("OpenRouter authorization was denied.")
            code = parameters.get("code") or ""
        else:
            value = prompt_task.result().strip()
            try:
                code = urlsplit(value).query
                code = parse_qs(code).get("code", [""])[0]
            except ValueError:
                code = ""
            if not code:
                code = value
        if not code:
            raise OAuthError("OpenRouter returned no authorization code.")
        token = await self._request_json(
            "POST",
            "https://openrouter.ai/api/v1/auth/keys",
            provider="OpenRouter",
            json_body={
                "code": code,
                "code_verifier": verifier,
                "code_challenge_method": "S256",
            },
        )
        key = _required_string(token, "key")
        return {
            "access": key,
            "refresh": "",
            "expires_at": 2**63 - 1,
            "extra": {},
        }

    async def _anthropic_callback_server(
        self, expected_state: str, future: asyncio.Future[dict[str, str]]
    ) -> asyncio.AbstractServer:
        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            status = "400 Bad Request"
            title = "Authentication failed"
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                if len(raw) > 16_384:
                    raise ValueError("request too large")
                request_line = raw.splitlines()[0].decode("ascii")
                method, target, _version = request_line.split(" ", 2)
                parsed = urlsplit(target)
                parameters = {
                    key: values[0]
                    for key, values in parse_qs(parsed.query).items()
                    if values
                }
                if method != "GET" or parsed.path != "/callback":
                    status = "404 Not Found"
                elif parameters.get("state") != expected_state:
                    title = "OAuth state mismatch"
                elif not parameters.get("code"):
                    title = "Authorization code is missing"
                else:
                    status = "200 OK"
                    title = "Authentication complete. You may close this window."
                    if not future.done():
                        future.set_result(parameters)
            except (ValueError, UnicodeDecodeError, asyncio.IncompleteReadError):
                pass
            except TimeoutError:
                pass
            body = (
                "<!doctype html><meta charset=utf-8><title>Switchboard OAuth</title>"
                f"<p>{title}</p>"
            ).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Cache-Control: no-store\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        try:
            return await asyncio.start_server(
                handle,
                host=self.settings.oauth_callback_host,
                port=_ANTHROPIC_CALLBACK_PORT,
                limit=16_384,
            )
        except OSError as exc:
            raise OAuthError(
                f"Anthropic OAuth needs local port {_ANTHROPIC_CALLBACK_PORT}; "
                "the port is already in use."
            ) from exc

    async def _login_anthropic(self, session: _LoginSession) -> dict[str, Any]:
        verifier, challenge = _pkce()
        state = verifier
        redirect_uri = f"http://localhost:{_ANTHROPIC_CALLBACK_PORT}/callback"
        loop = asyncio.get_running_loop()
        callback_future: asyncio.Future[dict[str, str]] = loop.create_future()
        server = await self._anthropic_callback_server(state, callback_future)
        authorize_url = "https://claude.ai/oauth/authorize?" + urlencode(
            {
                "code": "true",
                "client_id": _ANTHROPIC_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": (
                    "org:create_api_key user:profile user:inference "
                    "user:sessions:claude_code user:mcp_servers user:file_upload"
                ),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        await self._notify(
            session,
            {
                "type": "auth_url",
                "url": authorize_url,
                "instructions": "Complete Claude sign-in in the browser.",
            },
        )
        prompt_task = asyncio.create_task(
            self._prompt(
                session,
                prompt_type="manual_code",
                message=(
                    "If browser callback fails, paste the redirect URL or "
                    "authorization code."
                ),
                placeholder=redirect_uri,
            )
        )
        try:
            done, pending = await asyncio.wait(
                {callback_future, prompt_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for item in pending:
                item.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if callback_future in done:
                parameters = callback_future.result()
                code = parameters.get("code") or ""
                callback_state = parameters.get("state") or ""
            else:
                raw = prompt_task.result().strip()
                try:
                    parameters = {
                        key: values[0]
                        for key, values in parse_qs(urlsplit(raw).query).items()
                        if values
                    }
                except ValueError:
                    parameters = {}
                code = parameters.get("code") or raw.split("#", 1)[0]
                callback_state = parameters.get("state") or (
                    raw.split("#", 1)[1] if "#" in raw else state
                )
            if not code or callback_state != state:
                raise OAuthError("Anthropic OAuth code or state was invalid.")
            token = await self._request_json(
                "POST",
                "https://platform.claude.com/v1/oauth/token",
                provider="Anthropic",
                json_body={
                    "grant_type": "authorization_code",
                    "client_id": _ANTHROPIC_CLIENT_ID,
                    "code": code,
                    "state": callback_state,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            return self._anthropic_token_credential(token)
        finally:
            server.close()
            await server.wait_closed()

    @staticmethod
    def _anthropic_token_credential(
        token: dict[str, Any], *, previous_refresh: str = ""
    ) -> dict[str, Any]:
        access = _required_string(token, "access_token")
        refresh = token.get("refresh_token") or previous_refresh
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("Anthropic returned no refresh token.")
        expires_in = _positive_number(token, "expires_in", fallback=3600)
        return {
            "access": access,
            "refresh": refresh,
            "expires_at": _now_ms() + int(expires_in * 1_000) - _REFRESH_SKEW_MS,
            "extra": {"subscription": True},
        }

    async def _refresh_anthropic(self, credential: dict[str, Any]) -> dict[str, Any]:
        refresh = credential.get("refresh")
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("Anthropic refresh token is missing; sign in again.")
        token = await self._request_json(
            "POST",
            "https://platform.claude.com/v1/oauth/token",
            provider="Anthropic",
            json_body={
                "grant_type": "refresh_token",
                "client_id": _ANTHROPIC_CLIENT_ID,
                "refresh_token": refresh,
            },
        )
        return self._anthropic_token_credential(token, previous_refresh=refresh)

    @staticmethod
    def _kiro_start_url(value: Any) -> tuple[str, str]:
        if value in {None, ""}:
            return "https://view.awsapps.com/start", "builder-id"
        if not isinstance(value, str) or len(value) > 2_000:
            raise ValueError("Kiro Identity Center start URL is invalid.")
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        if (
            parsed.scheme != "https"
            or not (hostname == "awsapps.com" or hostname.endswith(".awsapps.com"))
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Kiro start URL must be an HTTPS awsapps.com URL.")
        return value.strip().rstrip("/"), "idc"

    async def _login_kiro(
        self, session: _LoginSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        unknown = set(payload) - {"start_url", "region"}
        if unknown:
            raise ValueError(f"Unknown Kiro login option: {sorted(unknown)[0]}")
        start_url, auth_method = self._kiro_start_url(payload.get("start_url"))
        region_value = payload.get("region")
        if region_value in {None, ""}:
            regions = ("us-east-1",) if auth_method == "builder-id" else _KIRO_REGIONS
        elif isinstance(region_value, str) and region_value in _KIRO_REGIONS:
            regions = (region_value,)
        else:
            raise ValueError("Kiro region is not in the supported allow-list.")

        registration: dict[str, Any] | None = None
        selected_region = ""
        for region in regions:
            endpoint = f"https://oidc.{region}.amazonaws.com"
            status, registered = await self._request_json_status(
                "POST",
                f"{endpoint}/client/register",
                provider="Kiro",
                json_body={
                    "clientName": "codex-provider-switchboard",
                    "clientType": "public",
                    "scopes": list(_KIRO_SCOPES),
                    "grantTypes": [
                        "urn:ietf:params:oauth:grant-type:device_code",
                        "refresh_token",
                    ],
                },
            )
            if status >= 400:
                continue
            client_id = registered.get("clientId")
            client_secret = registered.get("clientSecret")
            if not isinstance(client_id, str) or not isinstance(client_secret, str):
                continue
            status, device = await self._request_json_status(
                "POST",
                f"{endpoint}/device_authorization",
                provider="Kiro",
                json_body={
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "startUrl": start_url,
                },
            )
            if status >= 400:
                continue
            registration = {
                "endpoint": endpoint,
                "client_id": client_id,
                "client_secret": client_secret,
                "device": device,
            }
            selected_region = region
            break
        if registration is None:
            raise OAuthError("Kiro could not authorize the selected AWS identity.")

        device = registration["device"]
        user_code = _required_string(device, "userCode")
        verification_uri = _trusted_https_url(
            str(
                device.get("verificationUriComplete")
                or _required_string(device, "verificationUri")
            ),
            host_suffixes={"awsapps.com", "amazonaws.com"},
        )
        device_code = _required_string(device, "deviceCode")
        interval = _positive_number(device, "interval", fallback=5)
        expires_in = _positive_number(device, "expiresIn", fallback=600)
        await self._notify(
            session,
            {
                "type": "device_code",
                "user_code": user_code,
                "verification_uri": verification_uri,
                "interval_seconds": interval,
                "expires_in_seconds": expires_in,
            },
        )
        deadline = time.monotonic() + expires_in
        await asyncio.sleep(interval)
        while time.monotonic() < deadline:
            status, token = await self._request_json_status(
                "POST",
                f"{registration['endpoint']}/token",
                provider="Kiro",
                json_body={
                    "clientId": registration["client_id"],
                    "clientSecret": registration["client_secret"],
                    "deviceCode": device_code,
                    "grantType": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            if status < 400 and token.get("accessToken") and token.get("refreshToken"):
                return self._kiro_token_credential(
                    token,
                    client_id=registration["client_id"],
                    client_secret=registration["client_secret"],
                    region=selected_region,
                    auth_method=auth_method,
                )
            error = token.get("error")
            if error == "authorization_pending" or status >= 500:
                await asyncio.sleep(interval)
                continue
            if error == "slow_down":
                interval += 5
                await asyncio.sleep(interval)
                continue
            raise OAuthError(f"Kiro device authorization failed (HTTP {status}).")
        raise OAuthError("Kiro device authorization timed out.")

    @staticmethod
    def _kiro_token_credential(
        token: dict[str, Any],
        *,
        client_id: str,
        client_secret: str,
        region: str,
        auth_method: str,
        previous_refresh: str = "",
    ) -> dict[str, Any]:
        access = _required_string(token, "accessToken")
        refresh = token.get("refreshToken") or previous_refresh
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("Kiro returned no refresh token.")
        expires_in = _positive_number(token, "expiresIn", fallback=3600)
        return {
            "access": access,
            "refresh": refresh,
            "expires_at": _now_ms() + int(expires_in * 1_000) - _REFRESH_SKEW_MS,
            "extra": {
                "client_id": client_id,
                "client_secret": client_secret,
                "region": region,
                "auth_method": auth_method,
                "subscription": True,
            },
        }

    async def _refresh_kiro(self, credential: dict[str, Any]) -> dict[str, Any]:
        refresh = credential.get("refresh")
        extra = credential.get("extra")
        if not isinstance(refresh, str) or not refresh or not isinstance(extra, dict):
            raise OAuthError("Kiro refresh data is missing; sign in again.")
        client_id = extra.get("client_id")
        client_secret = extra.get("client_secret")
        region = extra.get("region")
        auth_method = extra.get("auth_method")
        if not all(
            isinstance(value, str) and value
            for value in (client_id, client_secret, region, auth_method)
        ):
            raise OAuthError("Kiro refresh data is incomplete; sign in again.")
        token = await self._request_json(
            "POST",
            f"https://oidc.{region}.amazonaws.com/token",
            provider="Kiro",
            json_body={
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh,
                "grantType": "refresh_token",
            },
        )
        return self._kiro_token_credential(
            token,
            client_id=client_id,
            client_secret=client_secret,
            region=region,
            auth_method=auth_method,
            previous_refresh=refresh,
        )
