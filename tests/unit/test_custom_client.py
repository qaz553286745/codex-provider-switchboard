from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from codex_provider_switchboard.infrastructure.config_store import ConfigStore
from codex_provider_switchboard.infrastructure.custom_client import (
    CustomResponsesClient,
)


def test_custom_client_models_quota_complete_and_stream(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("THIRD_PARTY_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer test_custom_key"
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-test", "name": "GPT Test"}]},
            )
        if request.method == "GET" and request.url.path == "/v1/account/quota":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "credits": {"used": 12.5, "total": 100},
                        "reset": "2026-08-01",
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/v1/responses":
            body = json.loads(request.content)
            assert body["model"] == "gpt-test"
            assert "client_metadata" not in body
            if body["stream"]:
                return httpx.Response(
                    200,
                    content=(
                        "event: response.completed\n"
                        'data: {"type":"response.completed"}\n\n'
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={"id": "resp_test", "object": "response", "status": "completed"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "custom": {
                "api_key": "test_custom_key",
                "base_url": "https://api.example.com/v1",
                "model_id": "gpt-test",
                "model_display_name": "GPT Test",
                "quota_path": "/account/quota",
                "quota_used_field": "data.credits.used",
                "quota_total_field": "data.credits.total",
                "quota_reset_field": "data.reset",
            }
        }
    )
    client = CustomResponsesClient(store, transport=httpx.MockTransport(handler))

    async def scenario() -> tuple[list[dict], dict, dict, bytes]:
        models = await client.get_models(force=True)
        quota = await client.quota()
        complete = await client.create_response(
            {"model": "ignored", "input": "hello", "client_metadata": {"x": 1}}
        )
        stream = b"".join(
            [chunk async for chunk in client.stream_response({"input": "hello"})]
        )
        return models, quota, complete, stream

    models, quota, complete, stream = asyncio.run(scenario())
    assert models == [{"id": "gpt-test", "displayName": "GPT Test"}]
    assert quota["remaining"] == 87.5
    assert quota["reset_at"] == "2026-08-01"
    assert complete["id"] == "resp_test"
    assert b"response.completed" in stream
    assert len(seen) == 4


def test_custom_config_rejects_remote_http_and_cross_origin_paths(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(ValueError, match="HTTPS"):
        store.update_from_api({"custom": {"base_url": "http://api.example.com/v1"}})
    with pytest.raises(ValueError, match="same-origin"):
        store.update_from_api(
            {"custom": {"quota_path": "https://collector.example/quota"}}
        )
    with pytest.raises(ValueError, match="compatibility_profile"):
        store.update_from_api({"custom": {"compatibility_profile": "unsafe_guess"}})


def test_custom_function_profile_restores_client_tool_shape(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("THIRD_PARTY_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "exec",
                "description": "Run JavaScript.",
                "parameters": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                },
            }
        ]
        assert "multi_agent" not in payload
        return httpx.Response(
            200,
            json={
                "id": "resp_tool",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec",
                        "call_id": "call_exec",
                        "arguments": '{"input":"text(true);"}',
                    }
                ],
            },
        )

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "custom": {
                "api_key": "test_custom_key",
                "base_url": "https://api.example.com/v1",
                "model_id": "gpt-test",
                "compatibility_profile": "function_only",
            }
        }
    )
    client = CustomResponsesClient(store, transport=httpx.MockTransport(handler))
    response = asyncio.run(
        client.create_response(
            {
                "input": "inspect",
                "tools": [
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Run JavaScript.",
                    }
                ],
                "multi_agent": {"enabled": True},
            }
        )
    )

    assert response["output"][0] == {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "call_exec",
        "input": "text(true);",
    }
