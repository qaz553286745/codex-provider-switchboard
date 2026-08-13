from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from codex_provider_switchboard.application.inspector import RequestInspector
from codex_provider_switchboard.infrastructure.config_store import ConfigStore
from codex_provider_switchboard.providers.direct import DirectProvider


def _events(raw: bytes) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in raw.decode().split("\n\n"):
        lines = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if lines:
            result.append(json.loads("\n".join(lines)))
    return result


class _AnthropicClient:
    async def stream_anthropic(
        self, _payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10, "output_tokens": 0}},
        }
        yield {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "call-1",
                "name": "read_file",
                "input": {},
            },
        }
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path"'},
        }
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ':"a.txt"}'},
        }
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "message_delta", "usage": {"output_tokens": 5}}
        yield {"type": "message_stop"}


def test_anthropic_tool_arguments_are_parsed_before_any_tool_event(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "active_provider": "direct",
            "direct": {"platform_id": "anthropic", "model_id": "claude-test"},
        }
    )
    provider = DirectProvider(
        store,
        _AnthropicClient(),  # type: ignore[arg-type]
        RequestInspector(),
    )
    body = {
        "input": "Inspect",
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        "stream": True,
    }

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in provider.stream(body)])

    events = _events(asyncio.run(scenario()))
    types = [event["type"] for event in events]
    argument_event = events[types.index("response.function_call_arguments.delta")]
    assert json.loads(argument_event["delta"]) == {"path": "a.txt"}
    assert types[-1] == "response.completed"
    completed = events[-1]["response"]
    assert completed["output"][0]["type"] == "function_call"


class _BrokenAnthropicClient:
    async def stream_anthropic(
        self, _payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("secret upstream detail")
        yield {}  # pragma: no cover


def test_unexpected_translation_failure_is_terminal_and_redacted(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {"direct": {"platform_id": "anthropic", "model_id": "claude-test"}}
    )
    provider = DirectProvider(
        store,
        _BrokenAnthropicClient(),  # type: ignore[arg-type]
        RequestInspector(),
    )

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in provider.stream({"input": "hi"})])

    raw = asyncio.run(scenario())
    events = _events(raw)
    assert events[-1]["type"] == "response.failed"
    assert "RuntimeError" in events[-1]["response"]["error"]["message"]
    assert b"secret upstream detail" not in raw


class _ResponsesClient:
    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None

    async def stream_responses(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        self.body = body
        yield (
            b'event: response.completed\ndata: {"type":"response.completed",'
            b'"response":{"id":"resp_1","status":"completed","output":[]}}\n\n'
        )


def test_direct_effort_following_can_be_disabled(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "direct": {
                "platform_id": "openai",
                "model_id": "gpt-test",
                "follow_codex_effort": False,
            }
        }
    )
    client = _ResponsesClient()
    provider = DirectProvider(
        store,
        client,  # type: ignore[arg-type]
        RequestInspector(),
    )

    async def scenario() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in provider.stream(
                    {
                        "input": "hello",
                        "reasoning": {"effort": "max"},
                        "reasoning_effort": "max",
                    }
                )
            ]
        )

    raw = asyncio.run(scenario())
    assert b"response.completed" in raw
    assert client.body is not None
    assert "reasoning" not in client.body
    assert "reasoning_effort" not in client.body


class _KiroCompletionClient:
    def __init__(self, rounds: list[str]) -> None:
        self.rounds = rounds
        self.payloads: list[dict[str, Any]] = []

    async def stream_kiro(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.payloads.append(payload)
        round_kind = self.rounds[len(self.payloads) - 1]
        tools = payload["conversationState"]["currentMessage"]["userInputMessage"][
            "userInputMessageContext"
        ]["tools"]
        final_name = next(
            tool["toolSpecification"]["name"]
            for tool in tools
            if tool["toolSpecification"]["name"].startswith(
                "switchboard_submit_final_answer"
            )
        )
        if round_kind == "progress":
            yield {"content": "I will run another check now."}
        elif round_kind == "final":
            yield {
                "name": final_name,
                "toolUseId": "internal-final-1",
                "input": json.dumps({"final_answer": "All checks completed."}),
                "stop": True,
            }
        elif round_kind == "normal_tool":
            yield {
                "name": "read_file",
                "toolUseId": "call-read-1",
                "input": json.dumps({"path": "status.txt"}),
                "stop": True,
            }
        else:  # pragma: no cover - test fixture misuse
            raise AssertionError(round_kind)


def _kiro_provider(tmp_path, client: _KiroCompletionClient) -> DirectProvider:
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "active_provider": "direct",
            "direct": {
                "platform_id": "kiro_direct",
                "model_id": "gpt-5.6-sol",
            },
        }
    )
    return DirectProvider(
        store,
        client,  # type: ignore[arg-type]
        RequestInspector(),
    )


def _kiro_body(*, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "input": "Check the server and finish the task.",
        "tools": tools or [],
        "stream": True,
    }


def test_kiro_internal_final_tool_becomes_a_real_final_answer(tmp_path) -> None:
    client = _KiroCompletionClient(["final"])
    provider = _kiro_provider(tmp_path, client)

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in provider.stream(_kiro_body())])

    events = _events(asyncio.run(scenario()))
    assert events[-1]["type"] == "response.completed"
    output = events[-1]["response"]["output"]
    final = next(item for item in output if item.get("phase") == "final_answer")
    assert final["content"][0]["text"] == "All checks completed."
    assert events[-1]["response"]["usage"]["output_tokens"] > 1
    assert all(item.get("name") != "switchboard_submit_final_answer" for item in output)
    current = client.payloads[0]["conversationState"]["currentMessage"][
        "userInputMessage"
    ]
    assert current["content"] == "Check the server and finish the task."
    assert "switchboard_completion_protocol" not in json.dumps(
        client.payloads[0], ensure_ascii=False
    )
    internal_tool = next(
        tool["toolSpecification"]
        for tool in current["userInputMessageContext"]["tools"]
        if tool["toolSpecification"]["name"].startswith(
            "switchboard_submit_final_answer"
        )
    )
    assert "Plain assistant text is progress commentary" in internal_tool["description"]


def test_kiro_plain_progress_is_continued_before_completion(tmp_path) -> None:
    client = _KiroCompletionClient(["progress", "final"])
    provider = _kiro_provider(tmp_path, client)

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in provider.stream(_kiro_body())])

    events = _events(asyncio.run(scenario()))
    assert events[-1]["type"] == "response.completed"
    assert len(client.payloads) == 2
    second_state = client.payloads[1]["conversationState"]
    assert second_state["history"][-1] == {
        "assistantResponseMessage": {"content": "I will run another check now."}
    }
    historical_context = second_state["history"][-2]["userInputMessage"].get(
        "userInputMessageContext", {}
    )
    assert "tools" not in historical_context
    current_tools = second_state["currentMessage"]["userInputMessage"][
        "userInputMessageContext"
    ]["tools"]
    assert current_tools


def test_kiro_repeated_plain_progress_fails_instead_of_false_completion(
    tmp_path,
) -> None:
    client = _KiroCompletionClient(["progress", "progress"])
    provider = _kiro_provider(tmp_path, client)

    async def scenario() -> bytes:
        return b"".join([chunk async for chunk in provider.stream(_kiro_body())])

    events = _events(asyncio.run(scenario()))
    assert events[-1]["type"] == "response.failed"
    assert (
        "without requesting a tool or submitting a final answer"
        in events[-1]["response"]["error"]["message"]
    )
    assert not any(event["type"] == "response.completed" for event in events)


def test_kiro_normal_tool_call_still_ends_the_agent_round(tmp_path) -> None:
    client = _KiroCompletionClient(["normal_tool"])
    provider = _kiro_provider(tmp_path, client)
    tool = {
        "type": "function",
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }

    async def scenario() -> bytes:
        return b"".join(
            [chunk async for chunk in provider.stream(_kiro_body(tools=[tool]))]
        )

    events = _events(asyncio.run(scenario()))
    assert events[-1]["type"] == "response.completed"
    output = events[-1]["response"]["output"]
    function_call = next(item for item in output if item["type"] == "function_call")
    assert function_call["name"] == "read_file"
    assert json.loads(function_call["arguments"]) == {"path": "status.txt"}
    assert not any(item.get("phase") == "final_answer" for item in output)
