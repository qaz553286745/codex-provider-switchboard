import asyncio
import json

import httpx

from codex_provider_switchboard.infrastructure.config_store import ConfigStore
from codex_provider_switchboard.infrastructure.cursor_client import (
    CursorClient,
    CursorModelSelection,
    CursorRun,
    apply_codex_effort,
    selection_from_config,
)


def test_codex_max_uses_only_cursor_advertised_parameter_values() -> None:
    model = {
        "id": "gpt-test",
        "parameters": [
            {
                "id": "reasoning",
                "values": [
                    {"value": "low"},
                    {"value": "high"},
                    {"value": "max"},
                ],
            },
            {
                "id": "fast",
                "values": [{"value": "false"}, {"value": "true"}],
            },
        ],
    }
    params = apply_codex_effort([{"id": "fast", "value": "false"}], model, "max")
    assert params == [
        {"id": "fast", "value": "false"},
        {"id": "reasoning", "value": "max"},
    ]


def test_selection_preserves_exact_cursor_variant_and_fingerprints_it() -> None:
    config = {
        "model_id": "gpt-test",
        "model_params": [{"id": "max_mode", "value": "false"}],
        "model_display_name": "GPT Test",
        "follow_codex_effort": True,
    }
    models = [
        {
            "id": "gpt-test",
            "parameters": [
                {
                    "id": "max_mode",
                    "values": [{"value": "false"}, {"value": "true"}],
                }
            ],
        }
    ]
    selection = selection_from_config(config, {"reasoning": {"effort": "max"}}, models)

    assert selection.request_value == {
        "id": "gpt-test",
        "params": [{"id": "max_mode", "value": "true"}],
    }
    assert len(selection.fingerprint) == 20


def test_cursor_client_uses_basic_auth_exact_model_payload_and_parses_sse(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/v1/agents":
            body = json.loads(request.content)
            assert body["model"] == {
                "id": "gpt-test",
                "params": [{"id": "reasoning_effort", "value": "max"}],
            }
            return httpx.Response(
                200,
                json={
                    "agent": {"id": "bc-agent"},
                    "run": {"id": "run-one"},
                },
            )
        if request.method == "GET" and request.url.path.endswith("/stream"):
            content = (
                'event: assistant\ndata: {"text":"hello"}\nid: one\n\n'
                "event: result\n"
                'data: {"runId":"run-one","status":"FINISHED",'
                '"text":"hello"}\n\n'
                "event: done\ndata: {}\n\n"
            )
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api({"cursor": {"api_key": "test_cursor_key"}})
    client = CursorClient(store, transport=httpx.MockTransport(handler))
    selection = CursorModelSelection(
        "gpt-test", (("reasoning_effort", "max"),), "GPT Test Max"
    )

    async def scenario() -> tuple[CursorRun, list]:
        run = await client.create_agent("prompt", selection)
        events = [event async for event in client.stream_run(run)]
        return run, events

    run, events = asyncio.run(scenario())
    assert run == CursorRun("bc-agent", "run-one", False)
    assert [event.event for event in events] == ["assistant", "result", "done"]
    assert events[0].event_id == "one"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert "test_cursor_key" not in seen[0].headers["authorization"]
