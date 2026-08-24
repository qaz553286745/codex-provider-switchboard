from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from codex_provider_switchboard.application.inspector import RequestInspector
from codex_provider_switchboard.application.service import SwitchboardService


class _Store:
    def read(self) -> dict[str, str]:
        return {"active_provider": "kiro"}


class _Provider:
    provider_id = "kiro"

    def __init__(self) -> None:
        self.called = False

    def model_id(self) -> str:
        return "gpt-5.6-sol"

    async def complete(self, _body: dict[str, Any]) -> Any:
        self.called = True
        raise AssertionError("loop guard should stop before the provider")

    async def stream(self, _body: dict[str, Any]):
        self.called = True
        raise AssertionError("loop guard should stop before the provider")
        yield b""  # pragma: no cover


def _loop_body() -> dict[str, Any]:
    target = "/root/reviewer"
    actions = [
        ("interrupt_agent", {"target": target}),
        ("followup_task", {"target": target, "message": "Continue"}),
        ("interrupt_agent", {"target": target}),
        ("followup_task", {"target": target, "message": "Continue"}),
        ("interrupt_agent", {"target": target}),
    ]
    return {
        "model": "gpt-5.6-sol",
        "input": [
            {"type": "message", "role": "user", "content": "Continue"},
            *[
                {
                    "type": "function_call",
                    "name": name,
                    "namespace": "collaboration",
                    "call_id": f"call-{index}",
                    "arguments": json.dumps(arguments),
                }
                for index, (name, arguments) in enumerate(actions)
            ],
        ],
    }


def _service() -> tuple[SwitchboardService, _Provider]:
    provider = _Provider()
    service = SwitchboardService.__new__(SwitchboardService)
    service.settings = SimpleNamespace(
        debug_requests=False,
        agent_loop_guard=True,
        agent_loop_restart_limit=2,
    )
    service.store = _Store()
    service.providers = {"kiro": provider}
    service.inspector = RequestInspector()
    return service, provider


def _events(raw: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.decode().split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def test_non_streaming_guard_completes_locally_without_provider_call() -> None:
    service, provider = _service()

    response = asyncio.run(service.complete(_loop_body()))

    assert not provider.called
    assert response.body["status"] == "completed"
    assert response.headers["X-Switchboard-Guard"] == "agent-control-loop"
    text = response.body["output"][0]["content"][0]["text"]
    assert "Stopped a repeated subagent-control loop" in text
    assert service.inspector.snapshot()["action"] == "agent_control_loop_stopped"


def test_streaming_guard_emits_clean_terminal_completion() -> None:
    service, provider = _service()

    async def scenario() -> bytes:
        iterator = service.stream_for("kiro", _loop_body())
        return b"".join([chunk async for chunk in iterator])

    events = _events(asyncio.run(scenario()))

    assert not provider.called
    assert events[-1]["type"] == "response.completed"
    assert not any(event["type"] == "response.failed" for event in events)
    output = events[-1]["response"]["output"]
    assert "Stopped a repeated subagent-control loop" in output[0]["content"][0]["text"]


def test_native_hosted_multi_agent_request_bypasses_local_loop_guard() -> None:
    service, _provider = _service()

    class _NativeStore:
        def read(self) -> dict[str, Any]:
            return {
                "active_provider": "custom",
                "custom": {"compatibility_profile": "native_codex"},
            }

    service.store = _NativeStore()
    body = _loop_body()
    body["multi_agent"] = {"enabled": True, "max_concurrent_subagents": 2}

    assert service._agent_loop_decision(body, "custom") is None

    body.pop("multi_agent")
    assert service._agent_loop_decision(body, "custom") is not None
