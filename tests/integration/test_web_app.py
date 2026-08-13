from __future__ import annotations

import asyncio
import gzip
import json
import re
import stat
import threading
import tomllib
import zlib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import zstandard
from starlette.testclient import TestClient

from codex_provider_switchboard.domain.bridge import clean_kiro_stdout
from codex_provider_switchboard.infrastructure.cursor_cli import CursorCliError
from codex_provider_switchboard.infrastructure.cursor_client import (
    CursorModelSelection,
    CursorRun,
    CursorStreamEvent,
)
from codex_provider_switchboard.providers.cursor import CursorProvider
from codex_provider_switchboard.runtime import build_runtime
from codex_provider_switchboard.settings import AppSettings
from codex_provider_switchboard.web.app import create_app


def _settings(tmp_path: Path, **changes: Any) -> AppSettings:
    value = AppSettings(
        host="127.0.0.1",
        port=8787,
        token=None,
        max_request_bytes=1_048_576,
        debug_requests=False,
        session_reuse=True,
        session_ttl_seconds=3_600,
        kiro_cli="kiro-cli",
        kiro_model="gpt-5.6-sol",
        kiro_workdir=tmp_path / "kiro-runtime",
        kiro_timeout_seconds=30,
        kiro_max_concurrency=1,
        kiro_max_prompt_bytes=1_048_576,
        kiro_context_recovery_prompt_bytes=512 * 1_024,
        kiro_max_output_bytes=1_048_576,
        kiro_allow_requested_model=False,
        kiro_tool_batching=True,
        cursor_cli="cursor-agent",
        cursor_workdir=tmp_path / "cursor-runtime",
        cursor_max_concurrency=1,
        cursor_max_prompt_bytes=1_048_576,
        cursor_max_output_bytes=1_048_576,
    )
    return replace(value, **changes)


def _decode_events(response: httpx.Response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in response.text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def test_direct_control_plane_is_safe_and_switches_native_openai(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer sk-direct-control"
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-direct"}]})
        if request.method == "POST" and request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"type":"response.created"}\n\n'
                    'data: {"type":"response.completed","response":'
                    '{"id":"resp_direct","status":"completed","output":[],"usage":'
                    '{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n'
                ),
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        direct_transport=httpx.MockTransport(handler),
    )
    app = create_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        platforms = client.get("/api/control/direct/platforms")
        assert platforms.status_code == 200
        assert {item["id"] for item in platforms.json()["platforms"]} >= {
            "openai",
            "openai_codex",
            "anthropic",
            "github_copilot",
            "xai",
            "openrouter",
            "kiro_direct",
        }

        configured = client.put(
            "/api/control/direct/api-key",
            json={"platform_id": "openai", "api_key": "sk-direct-control"},
        )
        assert configured.status_code == 200
        assert "sk-direct-control" not in configured.text

        selected = client.put(
            "/api/control/settings",
            json={
                "active_provider": "direct",
                "direct": {"platform_id": "openai", "model_id": "gpt-direct"},
            },
        )
        assert selected.status_code == 200

        tested = client.post("/api/control/direct/test", json={"platform_id": "openai"})
        assert tested.status_code == 200
        assert tested.json()["models"][0]["id"] == "gpt-direct"

        response = client.post("/v1/responses", json={"input": "hello", "stream": True})
        assert response.status_code == 200
        assert response.headers["x-switchboard-provider"] == "direct"
        assert _decode_events(response)[-1]["type"] == "response.completed"

        state = client.get("/api/control/state")
        assert "sk-direct-control" not in state.text
        assert state.json()["credentials"]["providers"]["openai"]["configured"]

        removed = client.delete("/api/control/direct/auth/openai")
        assert removed.status_code == 200
        assert not removed.json()["providers"]["direct"]["configured"]
    assert len(seen) == 2


def test_pi_kiro_import_is_control_protected_redacted_and_does_not_switch_provider(
    tmp_path,
) -> None:
    source = tmp_path / "pi-auth.json"
    source.write_text(
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
    source.chmod(0o600)
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    runtime.oauth._pi_auth_path = source
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = client.get("/api/control/state").json()["settings"]["active_provider"]
        cross_origin = client.post(
            "/api/control/direct/auth/kiro_direct/import",
            json={"source": "pi"},
            headers={"origin": "https://attacker.example"},
        )
        assert cross_origin.status_code == 403

        arbitrary_path = client.post(
            "/api/control/direct/auth/kiro_direct/import",
            json={"source": "/tmp/auth.json"},
        )
        assert arbitrary_path.status_code == 400

        imported = client.post(
            "/api/control/direct/auth/kiro_direct/import",
            json={"source": "pi"},
        )
        assert imported.status_code == 200
        assert imported.json()["settings"]["active_provider"] == before
        assert imported.json()["credentials"]["providers"]["kiro_direct"] == {
            "configured": True,
            "source": "credential_file",
            "type": "oauth",
            "expires_at": 4_102_444_800_000,
            "metadata": {
                "auth_method": "idc",
                "region": "us-east-1",
                "subscription": True,
            },
        }
        for secret in ("access-token", "refresh-token", "client-id", "client-secret"):
            assert secret not in imported.text


def test_oauth_callback_is_loopback_only_and_session_scoped(tmp_path) -> None:
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    app = create_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/control/direct/oauth/callback/not-a-session?code=secret",
            headers={"host": "attacker.example"},
        )
        assert response.status_code == 403
        assert "secret" not in response.text

        missing = client.get(
            "/api/control/direct/oauth/callback/not-a-session?code=secret"
        )
        assert missing.status_code == 400
        assert "secret" not in missing.text


class ScriptedKiroRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        answer = "first answer" if len(self.calls) == 1 else "second answer"
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps({"kind": "message", "text": answer})
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 7):
            yield wire[start : start + 7]

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        return clean_kiro_stdout(
            "".join([chunk async for chunk in self.stream(*args, **kwargs)])
        )

    async def latest_session_id(self, _workdir: Path) -> str:
        return "kiro-session-1"


class InterruptibleKiroRunner(ScriptedKiroRunner):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = threading.Event()
        self.first_cancelled = threading.Event()

    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        if len(self.calls) == 1:
            self.first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
            return

        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps({"kind": "message", "text": "steered answer"})
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 7):
            yield wire[start : start + 7]


class ToolCallingKiroRunner(ScriptedKiroRunner):
    def __init__(self) -> None:
        super().__init__()
        self.second_finished = False

    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        if len(self.calls) == 1:
            envelope = {
                "kind": "tool_calls",
                "calls": [{"name": "exec", "payload": "text(true);"}],
            }
        else:
            envelope = {
                "kind": "message",
                "text": "answer after tool output " + ("streaming " * 20),
            }
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps(envelope)
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 7):
            yield wire[start : start + 7]
        if len(self.calls) == 2:
            self.second_finished = True


class ContextOverflowThenAnswerRunner(ScriptedKiroRunner):
    def __init__(
        self,
        status: str = ("The context window has overflowed, summarizing the history..."),
        *,
        wrap_status: bool = False,
        repeat_status: bool = False,
    ) -> None:
        super().__init__()
        self.status = status
        self.wrap_status = wrap_status
        self.repeat_status = repeat_status

    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        if len(self.calls) == 1 or self.repeat_status:
            wire = self.status
            if self.wrap_status:
                wire = (
                    f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
                    + json.dumps({"kind": "message", "text": self.status})
                    + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
                )
            for start in range(0, len(wire), 7):
                yield wire[start : start + 7]
            return
        yield (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps({"kind": "message", "text": "recovered answer"})
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )


def test_kiro_stream_forwards_max_and_reuses_one_session(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    first_user = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "first user"}],
    }
    first_body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "same-codex-task"},
        "input": [first_user],
        "reasoning": {"effort": "max"},
        "stream": True,
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            first = await client.post("/v1/responses", json=first_body)
            first_output = _decode_events(first)[-1]["response"]["output"][0]
            second = await client.post(
                "/v1/responses",
                json={
                    **first_body,
                    "input": [
                        first_user,
                        first_output,
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "second user"}],
                        },
                    ],
                },
            )
        return first, second

    first, second = asyncio.run(scenario())
    first_events = _decode_events(first)
    second_events = _decode_events(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["x-switchboard-provider"] == "kiro"
    assert first_events[-1]["type"] == "response.completed"
    assert second_events[-1]["type"] == "response.completed"
    assert first_events[-1]["response"]["usage"] is None
    assert second_events[-1]["response"]["usage"] is None
    assert runner.calls[0]["effort"] == "max"
    assert runner.calls[0]["resume_id"] is None
    assert runner.calls[1]["resume_id"] is None
    assert runner.calls[1]["resume_latest"] is True
    assert "contains only the new items" in runner.calls[1]["prompt"]
    assert "second user" in runner.calls[1]["prompt"]
    assert "first user" not in runner.calls[1]["prompt"]


def test_kiro_context_overflow_retries_once_with_bounded_history(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ContextOverflowThenAnswerRunner()
    runtime = build_runtime(
        settings=_settings(
            tmp_path,
            max_request_bytes=256 * 1_024,
            kiro_max_prompt_bytes=256 * 1_024,
            kiro_context_recovery_prompt_bytes=16 * 1_024,
        ),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    old_text = "OLD_CONTEXT_" + ("x" * 60_000)
    newest_text = "CURRENT_QUESTION"
    body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "overflow-recovery-thread"},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": old_text}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old answer"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": newest_text}],
            },
        ],
        "stream": True,
    }

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post("/v1/responses", json=body)

    response = asyncio.run(scenario())
    events = _decode_events(response)
    assert response.status_code == 200
    assert len(runner.calls) == 2
    assert old_text in runner.calls[0]["prompt"]
    assert old_text not in runner.calls[1]["prompt"]
    assert newest_text in runner.calls[1]["prompt"]
    assert len(runner.calls[1]["prompt"].encode()) <= 16 * 1_024
    assert not any(
        "context window has overflowed" in json.dumps(event).lower() for event in events
    )
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"] is None
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == (
        "recovered answer"
    )


@pytest.mark.parametrize(
    ("status", "wrap_status"),
    [
        (
            "The context window has overflowed, summarizing the history...\n\n"
            "> CODEX_SWITCHBOARD_B...content truncated due to length",
            False,
        ),
        (
            "The context window has overflowed, summarizing the history...",
            True,
        ),
        ("CODEX_SWITCHBOARD_B...content truncated due to length", False),
    ],
)
def test_kiro_retryable_control_statuses_recover_without_sse_leak(
    monkeypatch,
    tmp_path,
    status: str,
    wrap_status: bool,
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ContextOverflowThenAnswerRunner(status, wrap_status=wrap_status)
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "retryable-status-thread"},
        "input": "answer without leaking upstream control text",
        "stream": True,
    }

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post("/v1/responses", json=body)

    response = asyncio.run(scenario())
    events = _decode_events(response)
    serialized_events = json.dumps(events)
    assert len(runner.calls) == 2
    assert status not in serialized_events
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == (
        "recovered answer"
    )


def test_kiro_repeated_truncation_ends_with_explicit_terminal_error(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ContextOverflowThenAnswerRunner(
        "CODEX_SWITCHBOARD_B...content truncated due to length",
        wrap_status=True,
        repeat_status=True,
    )
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "repeated-truncation-thread"},
        "input": "trigger a terminal retry result",
        "stream": True,
    }

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post("/v1/responses", json=body)

    response = asyncio.run(scenario())
    events = _decode_events(response)
    assert len(runner.calls) == 2
    assert not any(event["type"].startswith("response.output_") for event in events)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["code"] == "invalid_prompt"
    assert (
        "truncated its bridge output again"
        in (events[-1]["response"]["error"]["message"])
    )


def test_responses_websocket_streams_complete_json_events(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    received: list[dict[str, Any]] = []

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={
            "host": "127.0.0.1",
            "OpenAI-Beta": "responses_websockets=2026-02-06",
        },
    ) as websocket:
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "input": "hello",
                "reasoning": {"effort": "max"},
            }
        )
        while True:
            event = websocket.receive_json()
            received.append(event)
            if event["type"] == "response.completed":
                break

    assert received[0]["type"] == "response.created"
    assert received[-1]["type"] == "response.completed"
    assert runner.calls[0]["effort"] == "max"
    assert all(
        "CODEX_SWITCHBOARD_BRIDGE" not in json.dumps(event) for event in received
    )


def test_responses_websocket_prewarm_is_cached_without_provider_call(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    events: list[dict[str, Any]] = []

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={"host": "127.0.0.1"},
    ) as websocket:
        websocket.send_json(
            {
                "type": "response.create",
                "generate": False,
                "model": "gpt-5.6-sol",
                "input": "PREWARMED_USER_INPUT",
                "instructions": "PREWARMED_INSTRUCTIONS",
                "tools": [],
            }
        )
        created = websocket.receive_json()
        completed = websocket.receive_json()
        assert created["type"] == "response.created"
        assert completed["type"] == "response.completed"
        assert completed["response"]["id"] == created["response"]["id"]
        assert "PREWARMED_USER_INPUT" not in json.dumps([created, completed])
        assert runner.calls == []

        websocket.send_json(
            {
                "type": "response.create",
                "previous_response_id": completed["response"]["id"],
                "model": "gpt-5.6-sol",
                "input": [],
                "instructions": None,
                "tools": [],
            }
        )
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] == "response.completed":
                break

    assert len(runner.calls) == 1
    assert "PREWARMED_USER_INPUT" in runner.calls[0]["prompt"]
    assert "PREWARMED_INSTRUCTIONS" in runner.calls[0]["prompt"]
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "first answer"


def test_responses_websocket_cancel_interrupts_active_provider(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = InterruptibleKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses", headers={"host": "127.0.0.1"}
    ) as websocket:
        websocket.send_json(
            {"type": "response.create", "model": "gpt-5.6-sol", "input": "first"}
        )
        assert websocket.receive_json()["type"] == "response.created"
        assert runner.first_started.wait(1)
        websocket.send_json({"type": "response.cancel"})
        assert runner.first_cancelled.wait(1)
        websocket.send_json(
            {"type": "response.create", "model": "gpt-5.6-sol", "input": "after cancel"}
        )
        while True:
            event = websocket.receive_json()
            if event["type"] == "response.completed":
                break

    assert len(runner.calls) == 2
    assert event["response"]["output"][0]["content"][0]["text"] == ("steered answer")


def test_responses_websocket_new_create_steers_active_provider(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = InterruptibleKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses", headers={"host": "127.0.0.1"}
    ) as websocket:
        websocket.send_json(
            {"type": "response.create", "model": "gpt-5.6-sol", "input": "first"}
        )
        assert websocket.receive_json()["type"] == "response.created"
        assert runner.first_started.wait(1)
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "input": "模型权重太大, 请调整方向",
            }
        )
        while True:
            event = websocket.receive_json()
            if event["type"] == "response.completed":
                break

    assert runner.first_cancelled.is_set()
    assert len(runner.calls) == 2
    assert "模型权重太大, 请调整方向" in runner.calls[1]["prompt"]


def test_responses_websocket_generated_continuation_restores_tools_and_session(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ToolCallingKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={"host": "127.0.0.1"},
    ) as websocket:
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "client_metadata": {"thread_id": "websocket-tool-thread"},
                "input": [
                    {
                        "type": "additional_tools",
                        "tools": [
                            {
                                "name": "exec",
                                "description": "Accepts FREEFORM JavaScript input.",
                            }
                        ],
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "inspect the repository"}
                        ],
                    },
                ],
                "instructions": "Use an available repository tool when needed.",
                "tools": [],
            }
        )
        first_completed: dict[str, Any] | None = None
        while first_completed is None:
            event = websocket.receive_json()
            if event["type"] == "response.completed":
                first_completed = event["response"]

        tool_call = first_completed["output"][0]
        assert tool_call["type"] == "custom_tool_call"
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "previous_response_id": first_completed["id"],
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": tool_call["call_id"],
                        "output": "repository inspection result",
                    }
                ],
                "instructions": None,
                "tools": [],
            }
        )
        second_completed: dict[str, Any] | None = None
        streamed_before_upstream_finished = False
        while second_completed is None:
            event = websocket.receive_json()
            if (
                event["type"] == "response.output_text.delta"
                and not runner.second_finished
            ):
                streamed_before_upstream_finished = True
            if event["type"] == "response.completed":
                second_completed = event["response"]

    assert len(runner.calls) == 2
    assert runner.calls[1]["resume_id"] is None
    assert runner.calls[1]["resume_latest"] is True
    assert "contains only the new items" in runner.calls[1]["prompt"]
    assert "repository inspection result" in runner.calls[1]["prompt"]
    assert "inspect the repository" not in runner.calls[1]["prompt"]
    assert '"name":"exec"' in runner.calls[1]["prompt"]
    assert streamed_before_upstream_finished is True
    assert second_completed["output"][0]["content"][0][
        "text"
    ] == "answer after tool output " + ("streaming " * 20)


def test_responses_websocket_rejects_uncached_previous_response(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={"host": "127.0.0.1"},
    ) as websocket:
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "previous_response_id": "resp_not_cached",
                "input": [],
            }
        )
        error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["status"] == 400
    assert error["error"]["code"] == "previous_response_not_found"
    assert error["error"]["param"] == "previous_response_id"
    assert runner.calls == []


def test_native_remote_compaction_is_rejected_without_invoking_kiro(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={"host": "127.0.0.1"},
    ) as websocket:
        websocket.send_json(
            {
                "type": "response.create",
                "model": "gpt-5.6-sol",
                "input": [{"type": "compaction_trigger"}],
            }
        )
        error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["status"] == 400
    assert error["error"]["type"] == "unsupported_feature"
    assert "dedicated codex-provider-switchboard provider" in error["error"]["message"]
    assert runner.calls == []


def test_models_endpoint_supports_openai_and_codex_catalog_shapes(tmp_path) -> None:
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    app = create_app(runtime)

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.get("/v1/models")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["models"] == []
    assert response.json()["data"][0]["object"] == "model"


class ContaminatedResumeKiroRunner(ScriptedKiroRunner):
    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        if len(self.calls) == 2:
            stale = (
                "CODEX_SWITCHBOARD_BRIDGE_BEGIN_oldnonce\n"
                '{"kind":"tool_calls","calls":[{"name":"exec",'
                '"payload":"text(true);"}]}\n'
                "CODEX_SWITCHBOARD_BRIDGE_END_oldnonce"
            )
            envelope = {"kind": "message", "text": stale}
        else:
            envelope = {
                "kind": "message",
                "text": "remembered" if len(self.calls) == 1 else "recovered",
            }
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps(envelope)
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 3):
            yield wire[start : start + 3]

    async def latest_session_id(self, _workdir: Path) -> str:
        return "kiro-session-1" if len(self.calls) == 1 else "kiro-session-2"


def test_contaminated_kiro_resume_retries_fresh_without_sse_leak(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ContaminatedResumeKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    first_user = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "first user"}],
    }
    body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "same-codex-task"},
        "input": [first_user],
        "instructions": "The exec tool has a FREEFORM input grammar.",
        "stream": True,
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            first = await client.post("/v1/responses", json=body)
            first_output = _decode_events(first)[-1]["response"]["output"][0]
            second = await client.post(
                "/v1/responses",
                json={
                    **body,
                    "input": [
                        first_user,
                        first_output,
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "second user"}],
                        },
                    ],
                },
            )
        return first, second

    first, second = asyncio.run(scenario())
    assert _decode_events(first)[-1]["type"] == "response.completed"
    second_events = _decode_events(second)
    assert second_events[-1]["type"] == "response.completed"
    assert second_events[-1]["response"]["output"][0]["content"][0]["text"] == (
        "recovered"
    )
    assert "CODEX_SWITCHBOARD_BRIDGE" not in second.text
    assert len(runner.calls) == 3
    assert runner.calls[1]["resume_id"] is None
    assert runner.calls[1]["resume_latest"] is True
    assert runner.calls[2]["resume_id"] is None
    assert "contains only the new items" not in runner.calls[2]["prompt"]
    assert "first user" in runner.calls[2]["prompt"]
    assert "second user" in runner.calls[2]["prompt"]


class ChunkedToolKiroRunner:
    def __init__(self) -> None:
        self.finished = False

    async def stream(
        self,
        prompt: str,
        _model: str,
        _effort: str | None,
        **_kwargs: Any,
    ) -> AsyncIterator[str]:
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            '{"kind":"tool_calls","calls":[{"name":"weather",'
            '"payload":{"city":"Shanghai"}}]}\n'
            f"CODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for char in wire:
            yield char
        self.finished = True

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        return clean_kiro_stdout(
            "".join([chunk async for chunk in self.stream(*args, **kwargs)])
        )

    async def latest_session_id(self, _workdir: Path) -> str:
        return "tool-session"


def test_tool_call_events_wait_for_complete_chunked_envelope(tmp_path) -> None:
    runner = ChunkedToolKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    body = {
        "model": "gpt-5.6-sol",
        "input": "weather",
        "tools": [
            {
                "type": "function",
                "name": "weather",
                "parameters": {"type": "object"},
            }
        ],
        "stream": True,
    }

    async def scenario() -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        async for encoded in runtime.kiro_provider.stream(body):
            lines = encoded.decode().splitlines()
            data = next(line[6:] for line in lines if line.startswith("data: "))
            event = json.loads(data)
            if event["type"] not in {"response.created", "response.in_progress"}:
                assert runner.finished is True
            decoded.append(event)
        return decoded

    events = asyncio.run(scenario())
    assert not any(event["type"] == "response.output_text.delta" for event in events)
    assert events[-1]["type"] == "response.completed"
    output = events[-1]["response"]["output"][0]
    assert output["type"] == "function_call"
    assert output["name"] == "weather"
    assert json.loads(output["arguments"]) == {"city": "Shanghai"}
    assert "CODEX_SWITCHBOARD_BRIDGE" not in json.dumps(events)


class ChunkedCommentaryToolKiroRunner(ChunkedToolKiroRunner):
    async def stream(
        self,
        prompt: str,
        _model: str,
        _effort: str | None,
        **_kwargs: Any,
    ) -> AsyncIterator[str]:
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            '{"kind":"tool_calls","commentary":"我先查询天气。",'
            '"calls":[{"name":"weather",'
            '"payload":{"city":"Shanghai"}}]}\n'
            f"CODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for char in wire:
            yield char
        self.finished = True


def test_commentary_streams_before_complete_tool_envelope(tmp_path) -> None:
    runner = ChunkedCommentaryToolKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    body = {
        "model": "gpt-5.6-sol",
        "input": "weather",
        "tools": [
            {
                "type": "function",
                "name": "weather",
                "parameters": {"type": "object"},
            }
        ],
        "stream": True,
    }

    async def scenario() -> tuple[list[dict[str, Any]], bool]:
        decoded: list[dict[str, Any]] = []
        commentary_before_finish = False
        async for encoded in runtime.kiro_provider.stream(body):
            lines = encoded.decode().splitlines()
            data = next(line[6:] for line in lines if line.startswith("data: "))
            event = json.loads(data)
            if event["type"] == "response.output_text.delta":
                commentary_before_finish |= not runner.finished
            if event["type"] in {
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            }:
                assert runner.finished is True
            decoded.append(event)
        return decoded, commentary_before_finish

    events, commentary_before_finish = asyncio.run(scenario())
    completed = events[-1]["response"]

    assert commentary_before_finish is True
    assert completed["output"][0]["type"] == "message"
    assert completed["output"][0]["phase"] == "commentary"
    assert completed["output"][0]["content"][0]["text"] == "我先查询天气。"
    assert completed["output"][1]["type"] == "function_call"
    assert completed["output"][1]["name"] == "weather"
    assert "CODEX_SWITCHBOARD_BRIDGE" not in json.dumps(events)


class FakeCursorClient:
    backend_id = "cloud_api"
    runtime_name = "Cursor Cloud Agent"
    session_name = "Cursor agent"

    def __init__(self, selection: CursorModelSelection) -> None:
        self.selection = selection
        self.calls: list[dict[str, Any]] = []
        self.prompts: dict[str, str] = {}

    async def effective_selection(self, _body: dict[str, Any]) -> CursorModelSelection:
        return self.selection

    async def create_agent(
        self, prompt: str, selection: CursorModelSelection
    ) -> CursorRun:
        self.calls.append(
            {"action": "create_agent", "prompt": prompt, "selection": selection}
        )
        self.prompts["run-1"] = prompt
        return CursorRun("cursor-agent-1", "run-1", False)

    async def create_run(
        self,
        agent_id: str,
        prompt: str,
        _selection: CursorModelSelection | None = None,
    ) -> CursorRun:
        self.calls.append(
            {"action": "create_run", "agent_id": agent_id, "prompt": prompt}
        )
        self.prompts["run-2"] = prompt
        return CursorRun(agent_id, "run-2", True)

    async def stream_run(self, run: CursorRun) -> AsyncIterator[CursorStreamEvent]:
        prompt = self.prompts[run.run_id]
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        answer = "cursor first" if run.run_id == "run-1" else "cursor second"
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps({"kind": "message", "text": answer})
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 8):
            yield CursorStreamEvent("assistant", {"text": wire[start : start + 8]})
        yield CursorStreamEvent("result", {"status": "FINISHED", "text": wire})
        yield CursorStreamEvent("done", {})

    async def usage(self, _run: CursorRun) -> None:
        return None

    async def cancel_run(self, _run: CursorRun) -> None:
        raise AssertionError("Completed runs must not be cancelled.")


class FakeCursorCli(FakeCursorClient):
    backend_id = "cli"
    runtime_name = "Cursor Agent CLI"
    session_name = "Cursor CLI session"

    def __init__(self, selection: CursorModelSelection, settings: AppSettings) -> None:
        super().__init__(selection)
        self.settings = settings


class CommentaryToolCursorClient(FakeCursorClient):
    def __init__(self, selection: CursorModelSelection) -> None:
        super().__init__(selection)
        self.finished = False

    async def stream_run(self, run: CursorRun) -> AsyncIterator[CursorStreamEvent]:
        prompt = self.prompts[run.run_id]
        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            '{"kind":"tool_calls","commentary":"我先读取状态。",'
            '"calls":[{"name":"status","payload":{}}]}\n'
            f"CODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for char in wire:
            yield CursorStreamEvent("assistant", {"text": char})
        self.finished = True
        yield CursorStreamEvent("result", {"status": "FINISHED", "text": wire})


def test_cursor_commentary_streams_before_complete_tool_envelope(tmp_path) -> None:
    selection = CursorModelSelection("gpt-test", (), "GPT Test")
    fake = CommentaryToolCursorClient(selection)
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    runtime.store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {"backend": "cloud_api", "api_key": "test_cursor_key"},
        }
    )
    runtime.cursor_provider.client = fake  # type: ignore[assignment]
    body = {
        "input": "status",
        "tools": [
            {
                "type": "function",
                "name": "status",
                "parameters": {"type": "object"},
            }
        ],
        "stream": True,
    }

    async def scenario() -> tuple[list[dict[str, Any]], bool]:
        events: list[dict[str, Any]] = []
        commentary_before_finish = False
        async for encoded in runtime.cursor_provider.stream(body):
            data = next(
                line[6:]
                for line in encoded.decode().splitlines()
                if line.startswith("data: ")
            )
            event = json.loads(data)
            if event["type"] == "response.output_text.delta":
                commentary_before_finish |= not fake.finished
            if event["type"] in {
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            }:
                assert fake.finished is True
            events.append(event)
        return events, commentary_before_finish

    events, commentary_before_finish = asyncio.run(scenario())
    output = events[-1]["response"]["output"]

    assert commentary_before_finish is True
    assert output[0]["phase"] == "commentary"
    assert output[0]["content"][0]["text"] == "我先读取状态。"
    assert output[1]["type"] == "function_call"
    assert output[1]["name"] == "status"


def test_cursor_cli_session_profile_does_not_reuse_legacy_ask_mapping() -> None:
    body = {"client_metadata": {"thread_id": "codex-task"}}
    selection = CursorModelSelection("cursor-model", (), "Cursor Model")

    cached = CursorProvider._cache_body(body, selection, "cli")

    assert cached["client_metadata"]["thread_id"].startswith(
        "cursor-provider:cli:agent-delegation-v1:"
    )


def test_cursor_cli_trims_history_to_selected_model_context(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        max_request_bytes=2 * 1_048_576,
        cursor_max_prompt_bytes=4 * 1_048_576,
    )
    selection = CursorModelSelection("cursor-small", (), "Cursor Small 272K", 272_000)
    fake = FakeCursorCli(selection, settings)
    runtime = build_runtime(settings=settings, config_path=tmp_path / "config.json")
    runtime.store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {"backend": "cli", "api_key": "test_cursor_key"},
        }
    )
    runtime.cursor_provider.cli_runner = fake  # type: ignore[assignment]
    app = create_app(runtime)
    body = {
        "model": "gpt-5.6-sol",
        "stream": True,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "old:" + "x" * 650_000}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old answer"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "latest question"}],
            },
        ],
    }

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post("/v1/responses", json=body)

    response = asyncio.run(scenario())
    events = _decode_events(response)
    prompt = fake.calls[0]["prompt"]
    assert events[-1]["type"] == "response.completed"
    assert len(prompt.encode("utf-8")) <= 544_000
    assert '"history_truncation":{"applied":true' in prompt
    assert "latest question" in prompt
    assert "old:" not in prompt


class FirstOutputTimeoutCursorCli(FakeCursorCli):
    async def stream_run(self, _run: CursorRun) -> AsyncIterator[CursorStreamEvent]:
        if False:
            yield CursorStreamEvent("assistant", {"text": ""})
        raise CursorCliError(
            "Cursor CLI produced no assistant output within 120 seconds.",
            status_code=400,
        )

    async def cancel_run(self, _run: CursorRun) -> None:
        return None


def test_cursor_cli_first_output_timeout_is_terminal_invalid_prompt(tmp_path) -> None:
    settings = _settings(tmp_path)
    selection = CursorModelSelection("cursor-small", (), "Cursor Small", 272_000)
    fake = FirstOutputTimeoutCursorCli(selection, settings)
    runtime = build_runtime(settings=settings, config_path=tmp_path / "config.json")
    runtime.store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {"backend": "cli", "api_key": "test_cursor_key"},
        }
    )
    runtime.cursor_provider.cli_runner = fake  # type: ignore[assignment]
    app = create_app(runtime)

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post(
                "/v1/responses", json={"input": "hello", "stream": True}
            )

    response = asyncio.run(scenario())
    events = _decode_events(response)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
    assert events[-1]["response"]["error"]["code"] == "invalid_prompt"


def test_cursor_stream_reuses_agent_for_same_codex_task(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    selection = CursorModelSelection(
        "gpt-test", (("reasoning_effort", "max"),), "GPT Test · Max"
    )
    fake = FakeCursorClient(selection)
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    runtime.store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {
                "backend": "cloud_api",
                "api_key": "test_cursor_key",
            },
        }
    )
    runtime.cursor_provider.client = fake  # type: ignore[assignment]
    app = create_app(runtime)
    first_user = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "first user"}],
    }
    first_body = {
        "client_metadata": {"thread_id": "same-codex-task"},
        "input": [first_user],
        "stream": True,
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            first = await client.post("/v1/responses", json=first_body)
            first_output = _decode_events(first)[-1]["response"]["output"][0]
            second = await client.post(
                "/v1/responses",
                json={
                    **first_body,
                    "input": [
                        first_user,
                        first_output,
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "second user"}],
                        },
                    ],
                },
            )
        return first, second

    first, second = asyncio.run(scenario())
    assert _decode_events(first)[-1]["type"] == "response.completed"
    assert _decode_events(second)[-1]["type"] == "response.completed"
    assert [call["action"] for call in fake.calls] == [
        "create_agent",
        "create_run",
    ]
    assert fake.calls[1]["agent_id"] == "cursor-agent-1"
    assert "contains only the new items" in fake.calls[1]["prompt"]
    assert "second user" in fake.calls[1]["prompt"]
    assert "first user" not in fake.calls[1]["prompt"]


def test_control_api_never_returns_key_and_rejects_cross_origin_host(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    config_path = tmp_path / "settings" / "config.json"
    runtime = build_runtime(settings=_settings(tmp_path), config_path=config_path)
    app = create_app(runtime)

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            saved = await client.put(
                "/api/control/settings",
                json={"cursor": {"api_key": "test_cursor_key"}},
            )
            state = await client.get("/api/control/state")
            rejected = await client.put(
                "/api/control/settings",
                json={"active_provider": "cursor"},
                headers={"origin": "https://malicious.example"},
            )
        return saved, state, rejected

    saved, state, rejected = asyncio.run(scenario())
    assert saved.status_code == 200
    assert state.status_code == 200
    assert rejected.status_code == 403
    assert "test_cursor_key" not in saved.text
    assert "test_cursor_key" not in state.text
    assert state.json()["settings"]["cursor"]["api_key_configured"] is True
    assert state.json()["settings"]["cursor"]["backend"] == "cli"
    assert "default-src 'self'" in state.headers["content-security-policy"]
    assert state.headers["x-content-type-options"] == "nosniff"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    async def hostile_host() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.get(
                "/api/control/state", headers={"host": "malicious.example"}
            )

    assert asyncio.run(hostile_host()).status_code == 401


def test_request_size_and_content_type_are_enforced(tmp_path) -> None:
    runtime = build_runtime(
        settings=_settings(tmp_path, max_request_bytes=128),
        config_path=tmp_path / "config.json",
    )
    app = create_app(runtime)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            too_large = await client.post("/v1/responses", json={"input": "x" * 256})
            wrong_type = await client.post("/v1/responses", content='{"input":"hello"}')
        return too_large, wrong_type

    too_large, wrong_type = asyncio.run(scenario())
    assert too_large.status_code == 413
    assert wrong_type.status_code == 415


def test_http_fallback_decodes_supported_request_compression(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    raw = json.dumps({"input": "compressed fallback" + ("x" * 32_768)}).encode()
    raw_deflater = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    requests = [
        ("gzip", gzip.compress(raw)),
        ("deflate", zlib.compress(raw)),
        ("deflate", raw_deflater.compress(raw) + raw_deflater.flush()),
        ("zstd", zstandard.ZstdCompressor().compress(raw)),
        (
            "zstd",
            zstandard.ZstdCompressor(write_content_size=False).compress(raw),
        ),
    ]

    async def scenario() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return [
                await client.post(
                    "/v1/responses",
                    content=encoded,
                    headers={
                        "content-type": "application/json",
                        "content-encoding": encoding,
                    },
                )
                for encoding, encoded in requests
            ]

    responses = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [
        200,
        200,
        200,
        200,
        200,
    ]
    assert len(runner.calls) == 5


def test_http_compression_errors_are_explicit_and_bounded(tmp_path) -> None:
    runtime = build_runtime(
        settings=_settings(tmp_path, max_request_bytes=256),
        config_path=tmp_path / "config.json",
    )
    app = create_app(runtime)
    oversized_json = json.dumps({"input": "x" * 2_000}).encode()
    oversized = gzip.compress(oversized_json)
    oversized_zstd = zstandard.ZstdCompressor().compress(oversized_json)
    valid_zstd = zstandard.ZstdCompressor().compress(b'{"input":"ok"}')

    async def scenario() -> tuple[httpx.Response, ...]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            too_large = await client.post(
                "/v1/responses",
                content=oversized,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
            )
            zstd_too_large = await client.post(
                "/v1/responses",
                content=oversized_zstd,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "zstd",
                },
            )
            malformed = await client.post(
                "/v1/responses",
                content=b"not-a-zstd-frame",
                headers={
                    "content-type": "application/json",
                    "content-encoding": "zstd",
                },
            )
            trailing = await client.post(
                "/v1/responses",
                content=valid_zstd + b"trailing-data",
                headers={
                    "content-type": "application/json",
                    "content-encoding": "zstd",
                },
            )
            truncated = await client.post(
                "/v1/responses",
                content=valid_zstd[:-1],
                headers={
                    "content-type": "application/json",
                    "content-encoding": "zstd",
                },
            )
            unsupported = await client.post(
                "/v1/responses",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-encoding": "br",
                },
            )
        return (
            too_large,
            zstd_too_large,
            malformed,
            trailing,
            truncated,
            unsupported,
        )

    too_large, zstd_too_large, malformed, trailing, truncated, unsupported = (
        asyncio.run(scenario())
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["message"] == (
        "Decompressed request body is too large."
    )
    assert zstd_too_large.status_code == 413
    assert malformed.status_code == 400
    assert malformed.json()["error"]["message"] == (
        "Request body compression is invalid."
    )
    assert trailing.status_code == 400
    assert truncated.status_code == 400
    assert unsupported.status_code == 415


def test_websocket_validation_error_has_terminal_http_status(tmp_path) -> None:
    runtime = build_runtime(
        settings=_settings(tmp_path), config_path=tmp_path / "config.json"
    )
    app = create_app(runtime)

    with TestClient(app, base_url="http://127.0.0.1").websocket_connect(
        "/v1/responses",
        headers={"host": "127.0.0.1"},
    ) as websocket:
        websocket.send_json({"type": "unsupported", "input": "hello"})
        event = websocket.receive_json()

    assert event["type"] == "error"
    assert event["status"] == 400
    assert event["error"]["code"] == "invalid_event"


def test_kiro_overlimit_stream_is_terminal_without_invocation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    runner = ScriptedKiroRunner()
    runtime = build_runtime(
        settings=_settings(
            tmp_path,
            max_request_bytes=32_768,
            kiro_max_prompt_bytes=4_096,
        ),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post(
                "/v1/responses",
                json={"input": "x" * 10_000, "stream": True},
            )

    response = asyncio.run(scenario())
    events = _decode_events(response)
    assert response.status_code == 200
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
    assert events[-1]["response"]["error"]["code"] == "invalid_prompt"
    assert "Start a new task" in events[-1]["response"]["error"]["message"]
    assert runner.calls == []


class RejectingCursorCli:
    backend_id = "cli"
    runtime_name = "Cursor Agent CLI"
    session_name = "Cursor CLI session"

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.calls = 0

    async def effective_selection(self, _body: dict[str, Any]) -> CursorModelSelection:
        return CursorModelSelection("", (), "Cursor CLI default")

    async def create_agent(
        self, _prompt: str, _selection: CursorModelSelection
    ) -> CursorRun:
        self.calls += 1
        raise AssertionError("An overlimit prompt must not invoke Cursor CLI.")


def test_cursor_overlimit_stream_is_terminal_without_invocation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    settings = _settings(
        tmp_path,
        max_request_bytes=32_768,
        cursor_max_prompt_bytes=4_096,
    )
    runtime = build_runtime(
        settings=settings,
        config_path=tmp_path / "config.json",
    )
    runtime.store.update_from_api(
        {
            "active_provider": "cursor",
            "cursor": {"backend": "cli", "api_key": "test_cursor_key"},
        }
    )
    backend = RejectingCursorCli(settings)
    runtime.cursor_provider.cli_runner = backend  # type: ignore[assignment]
    app = create_app(runtime)

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            return await client.post(
                "/v1/responses",
                json={"input": "x" * 10_000, "stream": True},
            )

    response = asyncio.run(scenario())
    events = _decode_events(response)
    assert response.status_code == 200
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["code"] == "invalid_prompt"
    assert backend.calls == 0


def test_custom_provider_catalog_quota_and_sse_are_exposed_safely(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("THIRD_PARTY_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test_custom_key"
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})
        if request.method == "GET" and request.url.path == "/v1/quota":
            return httpx.Response(200, json={"used": 25, "total": 100})
        if request.method == "POST" and request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                content=(
                    'event: response.completed\ndata: {"type":"response.completed"}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        codex_config_path=tmp_path / ".codex" / "config.toml",
        custom_transport=httpx.MockTransport(handler),
    )
    app = create_app(runtime)

    async def scenario() -> tuple[httpx.Response, ...]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            saved = await client.put(
                "/api/control/settings",
                json={
                    "active_provider": "custom",
                    "custom": {
                        "api_key": "test_custom_key",
                        "base_url": "https://api.example.com/v1",
                        "model_id": "gpt-test",
                        "quota_path": "/quota",
                    },
                },
            )
            models = await client.get("/api/control/custom/models")
            quota = await client.get("/api/control/custom/quota?refresh=1")
            streamed = await client.post(
                "/v1/responses", json={"input": "hello", "stream": True}
            )
            state = await client.get("/api/control/state")
        return saved, models, quota, streamed, state

    saved, models, quota, streamed, state = asyncio.run(scenario())
    assert saved.status_code == 200
    assert models.json()["models"][0]["id"] == "gpt-test"
    assert quota.json()["remaining"] == 75
    assert streamed.headers["x-switchboard-provider"] == "custom"
    assert "response.completed" in streamed.text
    assert "test_custom_key" not in saved.text
    assert "test_custom_key" not in state.text


def test_codex_config_control_requires_confirmation_and_restores_backup(
    tmp_path,
) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    original = b'[plugins."keep@example"]\nenabled = true\n'
    config_path.write_bytes(original)
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "switchboard.json",
        codex_config_path=config_path,
    )
    app = create_app(runtime)

    async def scenario() -> tuple[httpx.Response, ...]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            rejected = await client.post(
                "/api/control/codex-config/enable",
                json={"confirmation": "yes", "model": "gpt-5.6-sol"},
            )
            enabled = await client.post(
                "/api/control/codex-config/enable",
                json={"confirmation": "ENABLE", "model": "gpt-5.6-sol"},
            )
            status = await client.get("/api/control/codex-config")
            restored = await client.post(
                "/api/control/codex-config/disable",
                json={"confirmation": "RESTORE"},
            )
        return rejected, enabled, status, restored

    rejected, enabled, status, restored = asyncio.run(scenario())
    assert rejected.status_code == 400
    assert enabled.status_code == 200
    assert status.json()["active"] is True
    assert restored.json()["active"] is False
    assert tomllib.loads(config_path.read_text()) == tomllib.loads(original.decode())


def test_codex_config_control_can_disable_without_backup(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text('model = "old"\n[desktop]\ncodeFontSize = 13\n')
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "switchboard.json",
        codex_config_path=config_path,
    )
    app = create_app(runtime)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            enabled = await client.post(
                "/api/control/codex-config/enable",
                json={"confirmation": "ENABLE", "model": "gpt-5.6-sol"},
            )
            Path(enabled.json()["backup_path"]).unlink()
            disabled = await client.post(
                "/api/control/codex-config/disable",
                json={"confirmation": "RESTORE"},
            )
        return enabled, disabled

    enabled, disabled = asyncio.run(scenario())
    assert enabled.status_code == 200
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    assert disabled.json()["restore_method"] == "managed_cleanup"
    parsed = tomllib.loads(config_path.read_text())
    assert "model_provider" not in parsed
    assert "model_providers" not in parsed
    assert parsed["desktop"]["codeFontSize"] == 13


class StallingResumeKiroRunner(ScriptedKiroRunner):
    def __init__(self) -> None:
        super().__init__()
        self.resumed_cancelled = threading.Event()

    async def stream(
        self, prompt: str, model: str, effort: str | None, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"prompt": prompt, "model": model, "effort": effort, **kwargs}
        )
        if len(self.calls) == 2:
            try:
                yield "hidden resumed output"
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.resumed_cancelled.set()
                raise
            return

        marker = re.search(r"CODEX_SWITCHBOARD_BRIDGE_BEGIN_([0-9a-f]+)", prompt)
        assert marker is not None
        nonce = marker.group(1)
        answer = "remembered" if len(self.calls) == 1 else "recovered"
        wire = (
            f"CODEX_SWITCHBOARD_BRIDGE_BEGIN_{nonce}\n"
            + json.dumps({"kind": "message", "text": answer})
            + f"\nCODEX_SWITCHBOARD_BRIDGE_END_{nonce}"
        )
        for start in range(0, len(wire), 7):
            yield wire[start : start + 7]


def test_stalled_resume_is_cancelled_and_retried_fresh(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(
        "codex_provider_switchboard.providers.kiro._RESUMED_SESSION_STALL_SECONDS",
        0.01,
    )
    runner = StallingResumeKiroRunner()
    runtime = build_runtime(
        settings=_settings(tmp_path),
        config_path=tmp_path / "config.json",
        kiro_runner=runner,  # type: ignore[arg-type]
    )
    app = create_app(runtime)
    first_user = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "first user"}],
    }
    body = {
        "model": "gpt-5.6-sol",
        "client_metadata": {"thread_id": "stalled-resume-task"},
        "input": [first_user],
        "stream": True,
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            first = await client.post("/v1/responses", json=body)
            first_output = _decode_events(first)[-1]["response"]["output"][0]
            second = await client.post(
                "/v1/responses",
                json={
                    **body,
                    "input": [
                        first_user,
                        first_output,
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "second user"}],
                        },
                    ],
                },
            )
        return first, second

    first, second = asyncio.run(scenario())
    assert _decode_events(first)[-1]["type"] == "response.completed"
    second_events = _decode_events(second)
    assert second_events[-1]["type"] == "response.completed"
    assert second_events[-1]["response"]["output"][0]["content"][0]["text"] == (
        "recovered"
    )
    assert runner.resumed_cancelled.is_set()
    assert len(runner.calls) == 3
    assert runner.calls[1]["resume_latest"] is True
    assert runner.calls[2]["resume_id"] is None
    assert runner.calls[2]["resume_latest"] is False
    assert "first user" in runner.calls[2]["prompt"]
    assert "second user" in runner.calls[2]["prompt"]
    assert "hidden resumed output" not in second.text
