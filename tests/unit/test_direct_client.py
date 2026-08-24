from __future__ import annotations

import asyncio
import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from codex_provider_switchboard.compatibility.responses import bind_transport_context
from codex_provider_switchboard.infrastructure.config_store import ConfigStore
from codex_provider_switchboard.infrastructure.credential_store import CredentialStore
from codex_provider_switchboard.infrastructure.direct_client import (
    DirectAPIError,
    DirectClient,
    _KiroEventDecoder,
)
from codex_provider_switchboard.infrastructure.oauth import OAuthLoginManager
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
    )


def _event_header(name: str, value: str) -> bytes:
    name_bytes = name.encode()
    value_bytes = value.encode()
    return (
        bytes([len(name_bytes)])
        + name_bytes
        + b"\x07"
        + struct.pack(">H", len(value_bytes))
        + value_bytes
    )


def _event_frame(payload: dict[str, Any]) -> bytes:
    headers = _event_header(":message-type", "event") + _event_header(
        ":event-type", "assistantResponseEvent"
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    total_size = 16 + len(headers) + len(encoded)
    prelude = struct.pack(">II", total_size, len(headers))
    prelude += struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + encoded
    return message + struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)


def test_kiro_event_stream_decoder_honors_frames_crc_and_chunk_boundaries() -> None:
    frame = _event_frame({"content": "hello"})
    decoder = _KiroEventDecoder()
    events: list[dict[str, Any]] = []
    for start in range(0, len(frame), 3):
        events.extend(decoder.feed(frame[start : start + 3]))
    decoder.finish()
    assert events == [
        {
            "content": "hello",
            "_switchboard_message_type": "event",
            "_switchboard_event_type": "assistantResponseEvent",
        }
    ]

    damaged = bytearray(frame)
    damaged[-1] ^= 1
    with pytest.raises(DirectAPIError, match="checksum"):
        _KiroEventDecoder().feed(bytes(damaged))


def test_kiro_direct_matches_pi_application_request_contract(tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/":
            return httpx.Response(
                200,
                json={"profiles": [{"arn": "arn:aws:codewhisperer:test-profile"}]},
            )
        return httpx.Response(
            200,
            # Pi requests application/json. Kiro labels the response accordingly
            # even though its body remains an AWS EventStream binary envelope.
            headers={"content-type": "application/json"},
            content=_event_frame({"content": "hello"}),
        )

    settings = _settings(tmp_path)
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "direct": {
                "platform_id": "kiro_direct",
                "model_id": "gpt-5.6-sol",
            }
        }
    )
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_oauth(
        "kiro_direct",
        access="kiro-access",
        refresh="kiro-refresh",
        expires_at=4_102_444_800_000,
        extra={
            "client_id": "kiro-client",
            "client_secret": "kiro-secret",
            "region": "us-east-1",
            "auth_method": "idc",
            "subscription": True,
        },
    )
    transport = httpx.MockTransport(handler)
    auth = OAuthLoginManager(settings, credentials, transport=transport)
    client = DirectClient(settings, store, auth, transport=transport)
    payload = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "agentTaskType": "vibe",
            "conversationId": "test-conversation",
            "currentMessage": {
                "userInputMessage": {
                    "content": "hello",
                    "modelId": "gpt-5.6-sol",
                    "origin": "KIRO_CLI",
                }
            },
        },
        "agentMode": "vibe",
    }

    async def scenario() -> list[dict[str, Any]]:
        return [event async for event in client.stream_kiro(payload)]

    assert asyncio.run(scenario())[0]["content"] == "hello"
    assert len(seen) == 2
    profile_request, generation_request = seen
    assert profile_request.headers["x-amz-target"] == (
        "AmazonCodeWhispererService.ListAvailableProfiles"
    )
    assert generation_request.url.path == "/generateAssistantResponse"
    assert generation_request.headers["accept"] == "application/json"
    assert generation_request.headers["content-type"] == ("application/x-amz-json-1.0")
    assert generation_request.headers["x-amz-target"] == (
        "AmazonCodeWhispererStreamingService.GenerateAssistantResponse"
    )
    assert generation_request.headers["x-amzn-codewhisperer-optout"] == "true"
    assert generation_request.headers["x-amzn-kiro-agent-mode"] == "vibe"
    assert generation_request.headers["amz-sdk-request"] == "attempt=1; max=1"
    assert re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
        generation_request.headers["amz-sdk-invocation-id"],
    )
    user_agent = generation_request.headers["user-agent"]
    assert re.fullmatch(
        r"aws-sdk-rust/1\.0\.0 ua/2\.1 os/other lang/rust "
        r"api/codewhispererstreaming#1\.28\.3 m/E app/AmazonQ-For-CLI "
        r"md/appVersion-1\.28\.3-[0-9a-f]{32}",
        user_agent,
    )
    assert generation_request.headers["x-amz-user-agent"] == user_agent
    assert json.loads(generation_request.content)["profileArn"] == (
        "arn:aws:codewhisperer:test-profile"
    )


def test_kiro_direct_retries_one_transient_http_failure(tmp_path) -> None:
    generation_attempts = 0
    invocation_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generation_attempts
        if request.url.path == "/":
            return httpx.Response(200, json={"profiles": []})
        generation_attempts += 1
        invocation_ids.append(request.headers["amz-sdk-invocation-id"])
        if generation_attempts == 1:
            return httpx.Response(
                500,
                json={
                    "message": (
                        "Encountered an unexpected error when processing the "
                        "request, please try again."
                    )
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=_event_frame({"content": "recovered"}),
        )

    settings = _settings(tmp_path)
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {"direct": {"platform_id": "kiro_direct", "model_id": "gpt-5.6-sol"}}
    )
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_oauth(
        "kiro_direct",
        access="kiro-access",
        refresh="kiro-refresh",
        expires_at=4_102_444_800_000,
        extra={"region": "us-east-1"},
    )
    transport = httpx.MockTransport(handler)
    client = DirectClient(
        settings,
        store,
        OAuthLoginManager(settings, credentials, transport=transport),
        transport=transport,
    )

    async def scenario() -> list[dict[str, Any]]:
        return [event async for event in client.stream_kiro({"conversationState": {}})]

    events = asyncio.run(scenario())
    assert events[0]["content"] == "recovered"
    assert generation_attempts == 2
    assert len(set(invocation_ids)) == 2


def test_kiro_direct_does_not_retry_permanent_http_failure(tmp_path) -> None:
    generation_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generation_attempts
        if request.url.path == "/":
            return httpx.Response(200, json={"profiles": []})
        generation_attempts += 1
        return httpx.Response(400, json={"message": "Invalid request."})

    settings = _settings(tmp_path)
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {"direct": {"platform_id": "kiro_direct", "model_id": "gpt-5.6-sol"}}
    )
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_oauth(
        "kiro_direct",
        access="kiro-access",
        refresh="kiro-refresh",
        expires_at=4_102_444_800_000,
        extra={"region": "us-east-1"},
    )
    transport = httpx.MockTransport(handler)
    client = DirectClient(
        settings,
        store,
        OAuthLoginManager(settings, credentials, transport=transport),
        transport=transport,
    )

    async def scenario() -> None:
        with pytest.raises(DirectAPIError, match="Invalid request"):
            async for _ in client.stream_kiro({"conversationState": {}}):
                pass

    asyncio.run(scenario())
    assert generation_attempts == 1


def test_openai_direct_client_uses_native_http_and_complete_sse_frames(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer sk-direct-test"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
        body = json.loads(request.content)
        assert body["model"] == "gpt-test"
        assert body["stream"] is True
        assert all(
            item.get("type") != "additional_tools"
            for item in body["input"]
            if isinstance(item, dict)
        )
        assert body["tools"][0]["name"] == "read_file"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'event: response.created\ndata: {"type":"response.created"}\n\n'
                "event: response.completed\n"
                'data: {"type":"response.completed","response":'
                '{"id":"resp_1","status":"completed","output":[]}}\n\n'
            ),
        )

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"direct": {"platform_id": "openai", "model_id": "gpt-test"}})
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_api_key("openai", "sk-direct-test")
    auth = OAuthLoginManager(
        _settings(tmp_path), credentials, transport=httpx.MockTransport(handler)
    )
    client = DirectClient(
        _settings(tmp_path),
        store,
        auth,
        transport=httpx.MockTransport(handler),
    )

    async def scenario() -> tuple[list[dict[str, Any]], bytes]:
        models = await client.get_models(force=True)
        stream = b"".join(
            [
                chunk
                async for chunk in client.stream_responses(
                    {
                        "input": [
                            {
                                "type": "additional_tools",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "read_file",
                                        "parameters": {"type": "object"},
                                    }
                                ],
                            },
                            {"type": "message", "role": "user", "content": "hello"},
                        ]
                    }
                )
            ]
        )
        return models, stream

    models, stream = asyncio.run(scenario())
    assert models == [{"id": "gpt-test", "displayName": "gpt-test"}]
    assert b"response.completed" in stream
    assert len(seen) == 2


def test_openai_native_forwards_multi_agent_beta_lineage_and_namespaces(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"type":"response.created"}\n\n'
                'data: {"type":"response.completed","response":'
                '{"id":"resp_native","status":"completed","output":[]}}\n\n'
            ),
        )

    settings = _settings(tmp_path)
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"direct": {"platform_id": "openai", "model_id": "gpt-test"}})
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_api_key("openai", "sk-test")
    transport = httpx.MockTransport(handler)
    client = DirectClient(
        settings,
        store,
        OAuthLoginManager(settings, credentials, transport=transport),
        transport=transport,
    )
    body = bind_transport_context(
        {
            "input": "inspect",
            "multi_agent": {"enabled": True, "max_concurrent_subagents": 2},
            "tools": [
                {
                    "type": "namespace",
                    "name": "multi_agent_v1",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
                {"type": "tool_search"},
            ],
        },
        {
            "openai-beta": "another_feature=v1",
            "x-openai-subagent": "worker",
            "x-codex-parent-thread-id": "parent-thread",
        },
    )

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in client.stream_responses(body)])

    assert b"response.completed" in asyncio.run(scenario())
    request = seen[0]
    beta = request.headers["openai-beta"]
    assert "another_feature=v1" in beta
    assert "responses_multi_agent=v1" in beta
    assert request.headers["x-openai-subagent"] == "worker"
    assert request.headers["x-codex-parent-thread-id"] == "parent-thread"
    payload = json.loads(request.content)
    assert payload["multi_agent"]["enabled"] is True
    assert payload["tools"][0]["type"] == "namespace"
    assert payload["tools"][1]["type"] == "tool_search"
    assert len(seen) == 1


def test_direct_responses_stream_requires_terminal_event(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"type":"response.created"}\n\n',
        )

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"direct": {"platform_id": "openai"}})
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_api_key("openai", "sk-direct-test")
    transport = httpx.MockTransport(handler)
    settings = _settings(tmp_path)
    auth = OAuthLoginManager(settings, credentials, transport=transport)
    client = DirectClient(settings, store, auth, transport=transport)

    async def scenario() -> None:
        async for _chunk in client.stream_responses({"input": "hello"}):
            pass

    with pytest.raises(DirectAPIError, match="terminal Responses event"):
        asyncio.run(scenario())


def test_anthropic_stream_requires_start_and_stop_events(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    responses = iter(
        [
            "",
            'data: {"type":"message_start","message":{}}\n\n',
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=next(responses),
        )

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"direct": {"platform_id": "anthropic"}})
    credentials = CredentialStore(tmp_path / "credentials.json")
    credentials.set_api_key("anthropic", "test-anthropic-key")
    transport = httpx.MockTransport(handler)
    settings = _settings(tmp_path)
    auth = OAuthLoginManager(settings, credentials, transport=transport)
    client = DirectClient(settings, store, auth, transport=transport)

    async def scenario() -> None:
        async for _event in client.stream_anthropic({"messages": []}):
            pass

    with pytest.raises(DirectAPIError, match="message_start"):
        asyncio.run(scenario())
    with pytest.raises(DirectAPIError, match="message_stop"):
        asyncio.run(scenario())
