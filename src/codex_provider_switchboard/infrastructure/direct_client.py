from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
import uuid
import zlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import __version__
from ..domain.bridge import collect_request_tools
from ..settings import AppSettings
from .config_store import ConfigStore
from .direct_catalog import curated_models, direct_platform
from .oauth import OAuthError, OAuthLoginManager, ResolvedCredential

_MAX_JSON_BYTES = 8 * 1_048_576
_MAX_SSE_EVENT_BYTES = 8 * 1_048_576
_KIRO_API_REGION_MAP = {
    "us-west-1": "us-east-1",
    "us-west-2": "us-east-1",
    "us-east-2": "us-east-1",
    "eu-west-1": "eu-central-1",
    "eu-west-2": "eu-central-1",
    "eu-west-3": "eu-central-1",
    "eu-north-1": "eu-central-1",
    "eu-south-1": "eu-central-1",
    "eu-south-2": "eu-central-1",
    "eu-central-2": "eu-central-1",
}
_KIRO_APPLICATION_VERSION = "1.28.3"
_KIRO_GENERATION_MAX_ATTEMPTS = 2
_KIRO_RETRY_DELAY_SECONDS = 0.25

logger = logging.getLogger(__name__)


def _kiro_generation_headers(access_token: str) -> dict[str, str]:
    request_suffix = uuid.uuid4().hex
    user_agent = (
        "aws-sdk-rust/1.0.0 ua/2.1 os/other lang/rust "
        f"api/codewhispererstreaming#{_KIRO_APPLICATION_VERSION} m/E "
        "app/AmazonQ-For-CLI "
        f"md/appVersion-{_KIRO_APPLICATION_VERSION}-{request_suffix}"
    )
    return {
        "Content-Type": "application/x-amz-json-1.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-Amz-Target": (
            "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
        ),
        "x-amzn-codewhisperer-optout": "true",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=1",
        "x-amzn-kiro-agent-mode": "vibe",
        "x-amz-user-agent": user_agent,
        "User-Agent": user_agent,
    }


class DirectAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _SSEDecoder:
    separator = re.compile(rb"\r?\n\r?\n")

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.buffer.extend(chunk)
        if len(self.buffer) > _MAX_SSE_EVENT_BYTES:
            raise DirectAPIError("Upstream SSE event exceeded the byte limit.")
        result: list[dict[str, Any]] = []
        while match := self.separator.search(self.buffer):
            block = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            event = self._decode(block)
            if event is not None:
                result.append(event)
        return result

    @staticmethod
    def _decode(block: bytes) -> dict[str, Any] | None:
        try:
            lines = block.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise DirectAPIError("Upstream SSE was not valid UTF-8.") from exc
        data: list[str] = []
        for line in lines:
            if line.startswith("data:"):
                part = line[5:]
                data.append(part[1:] if part.startswith(" ") else part)
        if not data or data == ["[DONE]"]:
            return None
        try:
            value = json.loads("\n".join(data))
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise DirectAPIError("Upstream SSE event was not valid JSON.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise DirectAPIError("Upstream SSE event had an invalid shape.")
        return value

    def finish(self) -> None:
        if self.buffer.strip():
            raise DirectAPIError("Upstream SSE ended with an incomplete event.")


def encode_response_event(event: dict[str, Any]) -> bytes:
    event_type = str(event.get("type") or "response.event")
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode()


class _KiroEventDecoder:
    """Decode AWS event-stream frames without an AWS SDK dependency."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    @staticmethod
    def _headers(raw: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        index = 0
        fixed_sizes = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
        while index < len(raw):
            name_size = raw[index]
            index += 1
            if name_size == 0 or index + name_size + 1 > len(raw):
                raise DirectAPIError("Kiro event-stream header was invalid.")
            try:
                name = raw[index : index + name_size].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DirectAPIError("Kiro event-stream header was invalid.") from exc
            index += name_size
            value_type = raw[index]
            index += 1
            if value_type in fixed_sizes:
                value_size = fixed_sizes[value_type]
                if index + value_size > len(raw):
                    raise DirectAPIError("Kiro event-stream header was truncated.")
                index += value_size
                continue
            if value_type not in {6, 7} or index + 2 > len(raw):
                raise DirectAPIError("Kiro event-stream header type was unsupported.")
            value_size = struct.unpack(">H", raw[index : index + 2])[0]
            index += 2
            if index + value_size > len(raw):
                raise DirectAPIError("Kiro event-stream header was truncated.")
            value = raw[index : index + value_size]
            index += value_size
            if value_type == 7:
                try:
                    headers[name] = value.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DirectAPIError(
                        "Kiro event-stream header was invalid."
                    ) from exc
        return headers

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.buffer.extend(chunk)
        events: list[dict[str, Any]] = []
        while True:
            if len(self.buffer) < 12:
                break
            total_size, header_size, expected_prelude_crc = struct.unpack(
                ">III", self.buffer[:12]
            )
            if total_size < 16 or total_size > _MAX_SSE_EVENT_BYTES:
                raise DirectAPIError("Kiro event-stream frame size was invalid.")
            if header_size > total_size - 16:
                raise DirectAPIError("Kiro event-stream header size was invalid.")
            if zlib.crc32(self.buffer[:8]) & 0xFFFFFFFF != expected_prelude_crc:
                raise DirectAPIError("Kiro event-stream prelude checksum failed.")
            if len(self.buffer) < total_size:
                break
            frame = bytes(self.buffer[:total_size])
            del self.buffer[:total_size]
            expected_message_crc = struct.unpack(">I", frame[-4:])[0]
            if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != expected_message_crc:
                raise DirectAPIError("Kiro event-stream message checksum failed.")
            headers = self._headers(frame[12 : 12 + header_size])
            raw = frame[12 + header_size : -4]
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise DirectAPIError(
                    "Kiro event-stream payload was not valid JSON."
                ) from exc
            if isinstance(value, dict):
                message_type = headers.get(":message-type")
                event_type = headers.get(":event-type")
                if message_type:
                    value["_switchboard_message_type"] = message_type
                if event_type:
                    value["_switchboard_event_type"] = event_type
                events.append(value)
            else:
                raise DirectAPIError("Kiro event-stream payload had an invalid shape.")
        return events

    def finish(self) -> None:
        if self.buffer:
            raise DirectAPIError("Kiro stream ended with an incomplete event.")


class DirectClient:
    """Native HTTP clients for Switchboard's fixed direct-provider catalog."""

    def __init__(
        self,
        settings: AppSettings,
        store: ConfigStore,
        auth: OAuthLoginManager,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.auth = auth
        self.transport = transport
        self._semaphore = asyncio.Semaphore(settings.direct_max_concurrency)
        self._models_cache: dict[str, tuple[str, float, list[dict[str, Any]]]] = {}
        self._models_locks: dict[str, asyncio.Lock] = {}
        self._profile_arns: dict[str, str] = {}

    def selection(self) -> tuple[str, str, int]:
        config = self.store.read()["direct"]
        return (
            str(config["platform_id"]),
            str(config["model_id"]),
            int(config["timeout_seconds"]),
        )

    def platform_id(self) -> str:
        return self.selection()[0]

    def model_id(self) -> str:
        return self.selection()[1]

    @staticmethod
    def _base_url(platform_id: str, credential: ResolvedCredential) -> str:
        if platform_id == "github_copilot":
            value = credential.extra.get("base_url")
            if isinstance(value, str) and value.startswith("https://"):
                parsed = httpx.URL(value)
                if parsed.host and parsed.host.endswith(".githubcopilot.com"):
                    return value.rstrip("/")
        if platform_id == "kiro_direct":
            region = credential.extra.get("region")
            if isinstance(region, str):
                api_region = _KIRO_API_REGION_MAP.get(region, region)
                if re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", api_region):
                    return f"https://q.{api_region}.amazonaws.com"
        return direct_platform(platform_id).base_url

    @staticmethod
    def _headers(
        platform_id: str,
        credential: ResolvedCredential,
        *,
        stream: bool = False,
        body: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"codex-provider-switchboard/{__version__}",
        }
        if platform_id == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if credential.credential_type == "oauth":
                headers["Authorization"] = f"Bearer {credential.token}"
                headers["anthropic-beta"] = (
                    "claude-code-20250219,oauth-2025-04-20,"
                    "fine-grained-tool-streaming-2025-05-14,"
                    "interleaved-thinking-2025-05-14"
                )
                headers["x-app"] = "cli"
            else:
                headers["x-api-key"] = credential.token
            return headers
        headers["Authorization"] = f"Bearer {credential.token}"
        if platform_id == "openai_codex":
            account_id = credential.extra.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise DirectAPIError("ChatGPT credential has no account ID.")
            headers["chatgpt-account-id"] = account_id
            headers["originator"] = "codex-provider-switchboard"
            headers["OpenAI-Beta"] = "responses=experimental"
        elif platform_id == "github_copilot":
            headers.update(
                {
                    "User-Agent": "GitHubCopilotChat/0.35.0",
                    "Editor-Version": "vscode/1.107.0",
                    "Editor-Plugin-Version": "copilot-chat/0.35.0",
                    "Copilot-Integration-Id": "vscode-chat",
                    "X-GitHub-Api-Version": "2026-06-01",
                    "Openai-Intent": "conversation-edits",
                    "X-Initiator": DirectClient._copilot_initiator(body or {}),
                }
            )
        return headers

    @staticmethod
    def _copilot_initiator(body: dict[str, Any]) -> str:
        input_value = body.get("input")
        if not isinstance(input_value, list) or not input_value:
            return "user"
        last = input_value[-1]
        if isinstance(last, dict) and last.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            return "agent"
        return "user"

    def _client(self, base_url: str, timeout_seconds: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                connect=20,
                read=timeout_seconds,
                write=60,
                pool=20,
            ),
            follow_redirects=False,
            transport=self.transport,
        )

    @staticmethod
    async def _read_limited(
        response: httpx.Response, limit: int = _MAX_JSON_BYTES
    ) -> bytes:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > limit:
                raise DirectAPIError(
                    "Direct provider response exceeded the byte limit."
                )
        return bytes(raw)

    @staticmethod
    def _error(status_code: int, raw: bytes, platform_name: str) -> DirectAPIError:
        message = f"{platform_name} returned HTTP {status_code}."
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            value = None
        if isinstance(value, dict):
            detail = value.get("error") or value.get("message")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if isinstance(detail, str) and detail.strip():
                message = f"{platform_name}: {detail.strip()[:500]}"
        return DirectAPIError(message, status_code=status_code)

    def _responses_payload(
        self, body: dict[str, Any], platform_id: str, model_id: str, *, stream: bool
    ) -> dict[str, Any]:
        payload = dict(body)
        payload.pop("client_metadata", None)
        payload.pop("generate", None)
        payload["model"] = model_id
        payload["stream"] = stream
        if platform_id != "openai_codex":
            input_value = payload.get("input")
            if isinstance(input_value, list):
                payload["input"] = [
                    item
                    for item in input_value
                    if not (
                        isinstance(item, dict)
                        and item.get("type") == "additional_tools"
                    )
                ]
            tools = collect_request_tools(body)
            if tools:
                payload["tools"] = [
                    {
                        key: value
                        for key, value in tool.items()
                        if key not in {"_namespace", "_wire_name"}
                    }
                    for tool in tools
                ]
            payload.pop("reasoning_effort", None)
        if platform_id in {"openai_codex", "openrouter"}:
            payload["store"] = False
        if platform_id == "openai_codex":
            payload["instructions"] = payload.get("instructions") or (
                "You are a helpful coding assistant."
            )
            include = payload.get("include")
            if not isinstance(include, list):
                include = []
            if "reasoning.encrypted_content" not in include:
                include = [*include, "reasoning.encrypted_content"]
            payload["include"] = include
            payload.setdefault("parallel_tool_calls", True)
        if platform_id == "openrouter":
            # OpenRouter's Responses implementation is stateless. The caller already
            # supplies reconstructed history, so forwarding a foreign response id is
            # both unnecessary and error-prone.
            payload.pop("previous_response_id", None)
        return payload

    async def stream_responses(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        platform_id, model_id, timeout_seconds = self.selection()
        platform = direct_platform(platform_id)
        if platform.protocol != "responses":
            raise DirectAPIError(f"{platform.name} does not use Responses protocol.")
        try:
            credential = await self.auth.resolve(platform_id)
        except OAuthError as exc:
            raise DirectAPIError(str(exc)) from exc
        base_url = self._base_url(platform_id, credential)
        payload = self._responses_payload(body, platform_id, model_id, stream=True)
        headers = self._headers(platform_id, credential, stream=True, body=body)
        decoder = _SSEDecoder()
        stream_bytes = 0
        terminal = False
        async with self._semaphore:
            try:
                async with (
                    self._client(base_url, timeout_seconds) as client,
                    client.stream(
                        "POST",
                        platform.response_path,
                        headers=headers,
                        json=payload,
                    ) as response,
                ):
                    if response.status_code >= 400:
                        raw = await self._read_limited(response)
                        raise self._error(response.status_code, raw, platform.name)
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        raise DirectAPIError(
                            f"{platform.name} did not return an SSE stream."
                        )
                    async for chunk in response.aiter_bytes():
                        stream_bytes += len(chunk)
                        if stream_bytes > self.settings.direct_max_output_bytes:
                            raise DirectAPIError(
                                f"{platform.name} stream exceeded the byte limit."
                            )
                        for event in decoder.feed(chunk):
                            event_type = event["type"]
                            if event_type in {"response.completed", "response.failed"}:
                                terminal = True
                            yield encode_response_event(event)
                    decoder.finish()
            except DirectAPIError:
                raise
            except httpx.HTTPError as exc:
                raise DirectAPIError(
                    f"{platform.name} request failed ({type(exc).__name__})."
                ) from exc
        if not terminal:
            raise DirectAPIError(
                f"{platform.name} stream ended before a terminal Responses event."
            )

    async def stream_anthropic(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        platform_id, _model_id, timeout_seconds = self.selection()
        if platform_id != "anthropic":
            raise DirectAPIError("Anthropic stream requested for another platform.")
        try:
            credential = await self.auth.resolve(platform_id)
        except OAuthError as exc:
            raise DirectAPIError(str(exc)) from exc
        platform = direct_platform(platform_id)
        headers = self._headers(platform_id, credential, stream=True)
        decoder = _SSEDecoder()
        saw_start = False
        saw_stop = False
        stream_bytes = 0
        async with self._semaphore:
            try:
                async with (
                    self._client(platform.base_url, timeout_seconds) as client,
                    client.stream(
                        "POST", platform.response_path, headers=headers, json=payload
                    ) as response,
                ):
                    if response.status_code >= 400:
                        raw = await self._read_limited(response)
                        raise self._error(response.status_code, raw, platform.name)
                    if (
                        "text/event-stream"
                        not in response.headers.get("content-type", "").lower()
                    ):
                        raise DirectAPIError("Anthropic did not return an SSE stream.")
                    async for chunk in response.aiter_bytes():
                        stream_bytes += len(chunk)
                        if stream_bytes > self.settings.direct_max_output_bytes:
                            raise DirectAPIError(
                                "Anthropic stream exceeded the byte limit."
                            )
                        for event in decoder.feed(chunk):
                            event_type = event.get("type")
                            saw_start |= event_type == "message_start"
                            saw_stop |= event_type == "message_stop"
                            if event_type == "error":
                                error = event.get("error")
                                message = (
                                    error.get("message")
                                    if isinstance(error, dict)
                                    else None
                                )
                                detail = str(message or "error")[:500]
                                raise DirectAPIError(
                                    f"Anthropic stream failed: {detail}"
                                )
                            yield event
                    decoder.finish()
            except DirectAPIError:
                raise
            except httpx.HTTPError as exc:
                raise DirectAPIError(
                    f"Anthropic request failed ({type(exc).__name__})."
                ) from exc
        if not saw_start:
            raise DirectAPIError("Anthropic stream ended before a message_start event.")
        if not saw_stop:
            raise DirectAPIError("Anthropic stream ended before message_stop.")

    async def _kiro_profile_arn(
        self,
        base_url: str,
        credential: ResolvedCredential,
        timeout_seconds: int,
    ) -> str | None:
        cached = self._profile_arns.get(base_url)
        if cached:
            return cached
        try:
            async with self._client(base_url, timeout_seconds) as client:
                response = await client.post(
                    "/",
                    headers={
                        "Content-Type": "application/x-amz-json-1.0",
                        "Authorization": f"Bearer {credential.token}",
                        "X-Amz-Target": (
                            "AmazonCodeWhispererService.ListAvailableProfiles"
                        ),
                    },
                    content=b"{}",
                )
            if response.status_code >= 400 or len(response.content) > _MAX_JSON_BYTES:
                return None
            value = response.json()
            profiles = value.get("profiles") if isinstance(value, dict) else None
            if isinstance(profiles, list):
                for profile in profiles:
                    arn = profile.get("arn") if isinstance(profile, dict) else None
                    if isinstance(arn, str) and arn:
                        self._profile_arns[base_url] = arn
                        return arn
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return None
        return None

    async def stream_kiro(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        platform_id, _model_id, timeout_seconds = self.selection()
        if platform_id != "kiro_direct":
            raise DirectAPIError("Kiro stream requested for another platform.")
        try:
            credential = await self.auth.resolve(platform_id)
        except OAuthError as exc:
            raise DirectAPIError(str(exc)) from exc
        platform = direct_platform(platform_id)
        base_url = self._base_url(platform_id, credential)
        profile_arn = await self._kiro_profile_arn(
            base_url, credential, timeout_seconds
        )
        if profile_arn:
            payload = {**payload, "profileArn": profile_arn}
        saw_event = False
        async with self._semaphore:
            for attempt in range(1, _KIRO_GENERATION_MAX_ATTEMPTS + 1):
                decoder = _KiroEventDecoder()
                stream_bytes = 0
                try:
                    async with (
                        self._client(base_url, timeout_seconds) as client,
                        client.stream(
                            "POST",
                            platform.response_path,
                            headers=_kiro_generation_headers(credential.token),
                            json=payload,
                        ) as response,
                    ):
                        if response.status_code >= 400:
                            raw = await self._read_limited(response)
                            retryable = response.status_code == 429 or (
                                500 <= response.status_code <= 599
                            )
                            will_retry = (
                                retryable
                                and not saw_event
                                and attempt < _KIRO_GENERATION_MAX_ATTEMPTS
                            )
                            log = logger.warning if will_retry else logger.error
                            log(
                                "Kiro Direct upstream HTTP failure status=%d "
                                "attempt=%d max_attempts=%d retry=%s",
                                response.status_code,
                                attempt,
                                _KIRO_GENERATION_MAX_ATTEMPTS,
                                will_retry,
                            )
                            if will_retry:
                                await asyncio.sleep(_KIRO_RETRY_DELAY_SECONDS)
                                continue
                            raise self._error(response.status_code, raw, platform.name)
                        async for chunk in response.aiter_bytes():
                            stream_bytes += len(chunk)
                            if stream_bytes > self.settings.direct_max_output_bytes:
                                raise DirectAPIError(
                                    "Kiro stream exceeded the byte limit."
                                )
                            for event in decoder.feed(chunk):
                                saw_event = True
                                yield event
                        decoder.finish()
                        break
                except DirectAPIError:
                    raise
                except httpx.HTTPError as exc:
                    raise DirectAPIError(
                        f"Kiro direct request failed ({type(exc).__name__})."
                    ) from exc
        if not saw_event:
            raise DirectAPIError("Kiro stream ended without any events.")

    @staticmethod
    def _normalize_models(payload: Any) -> list[dict[str, Any]]:
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise DirectAPIError("Model catalog did not contain a model list.")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items[:2_000]:
            if isinstance(item, str):
                model_id = item
                display_name = item
                description = ""
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                display_name = item.get("display_name") or item.get("name") or model_id
                description = item.get("description") or ""
            else:
                continue
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip()[:200]
            if model_id in seen:
                continue
            normalized: dict[str, Any] = {
                "id": model_id,
                "displayName": str(display_name or model_id)[:240],
            }
            if isinstance(description, str) and description.strip():
                normalized["description"] = description.strip()[:1_000]
            result.append(normalized)
            seen.add(model_id)
        if not result:
            raise DirectAPIError("Model catalog was empty.")
        return result

    async def get_models(
        self, platform_id: str | None = None, *, force: bool = False
    ) -> list[dict[str, Any]]:
        selected_platform = platform_id or self.platform_id()
        platform = direct_platform(selected_platform)
        curated = curated_models(selected_platform)
        if platform.models_path is None:
            return curated
        try:
            credential = await self.auth.resolve(selected_platform)
        except OAuthError as exc:
            raise DirectAPIError(str(exc)) from exc
        cache_key = hashlib.sha256(
            (
                selected_platform + "\0" + credential.source + "\0" + credential.token
            ).encode()
        ).hexdigest()
        cached = self._models_cache.get(selected_platform)
        now = time.monotonic()
        if not force and cached and cached[0] == cache_key and now - cached[1] < 300:
            return [dict(item) for item in cached[2]]
        lock = self._models_locks.setdefault(selected_platform, asyncio.Lock())
        async with lock:
            cached = self._models_cache.get(selected_platform)
            now = time.monotonic()
            if (
                not force
                and cached
                and cached[0] == cache_key
                and now - cached[1] < 300
            ):
                return [dict(item) for item in cached[2]]
            base_url = self._base_url(selected_platform, credential)
            headers = self._headers(selected_platform, credential)
            _platform_id, _model_id, timeout_seconds = self.selection()
            try:
                async with self._client(base_url, timeout_seconds) as client:
                    response = await client.get(platform.models_path, headers=headers)
            except httpx.HTTPError as exc:
                raise DirectAPIError(
                    f"Could not reach {platform.name} model catalog."
                ) from exc
            raw = response.content
            if len(raw) > _MAX_JSON_BYTES:
                raise DirectAPIError("Model catalog exceeded the byte limit.")
            if response.status_code >= 400:
                raise self._error(response.status_code, raw, platform.name)
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DirectAPIError("Model catalog returned invalid JSON.") from exc
            items = self._normalize_models(value)
            available = credential.extra.get("available_model_ids")
            if selected_platform == "github_copilot" and isinstance(available, list):
                allow = {item for item in available if isinstance(item, str)}
                filtered = [item for item in items if item["id"] in allow]
                if filtered:
                    items = filtered
            self._models_cache[selected_platform] = (cache_key, now, items)
            return [dict(item) for item in items]

    async def quota(self) -> dict[str, Any]:
        platform_id = self.platform_id()
        if platform_id != "openrouter":
            return {
                "status": "unsupported",
                "source": direct_platform(platform_id).name,
                "note": "This platform does not publish a compatible quota endpoint.",
            }
        try:
            credential = await self.auth.resolve(platform_id)
        except OAuthError as exc:
            raise DirectAPIError(str(exc)) from exc
        headers = self._headers(platform_id, credential)
        platform = direct_platform(platform_id)
        try:
            async with self._client(platform.base_url, 30) as client:
                response = await client.get("/auth/key", headers=headers)
        except httpx.HTTPError as exc:
            raise DirectAPIError("Could not reach OpenRouter quota endpoint.") from exc
        if response.status_code >= 400:
            raise self._error(response.status_code, response.content, platform.name)
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise DirectAPIError(
                "OpenRouter quota endpoint returned invalid JSON."
            ) from exc
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, dict):
            return {"status": "unknown", "source": "OpenRouter /auth/key"}
        limit = data.get("limit")
        used = data.get("usage")
        remaining = (
            float(limit) - float(used)
            if isinstance(limit, (int, float)) and isinstance(used, (int, float))
            else None
        )
        return {
            "status": "ok",
            "source": "OpenRouter /auth/key",
            "unit": "USD credits",
            "total": limit,
            "used": used,
            "remaining": remaining,
        }
