from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from codex_provider_switchboard.infrastructure.config_store import ConfigStore
from codex_provider_switchboard.infrastructure.cursor_cli import (
    CursorCliError,
    CursorCliRunner,
    cli_selection_from_config,
    cursor_prompt_byte_limit,
    parse_cursor_cli_models,
)
from codex_provider_switchboard.settings import AppSettings


def _settings(tmp_path: Path, executable: Path) -> AppSettings:
    return AppSettings(
        host="127.0.0.1",
        port=8787,
        token=None,
        max_request_bytes=1_048_576,
        debug_requests=False,
        session_reuse=True,
        session_ttl_seconds=3_600,
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
        cursor_cli=str(executable),
        cursor_workdir=tmp_path / "cursor",
        cursor_max_concurrency=1,
        cursor_max_prompt_bytes=1_048_576,
        cursor_max_output_bytes=1_048_576,
    )


def _fake_cursor_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "cursor-agent"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--list-models" in sys.argv:
    print("gpt-5.6-sol-high - GPT-5.6 Sol High")
    print("gpt-5.6-sol-max - GPT-5.6 Sol Max")
    raise SystemExit(0)

prompt = sys.stdin.read().strip()
if prompt != "stdin-only prompt" or prompt in sys.argv:
    raise SystemExit(9)
resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
session_id = resume or "cursor-cli-session"
events = [
    {"type": "system", "subtype": "init", "session_id": session_id,
     "model": "GPT-5.6 Sol 272K Max", "apiKeySource": "env"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hel"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "lo"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "hello", "session_id": session_id,
     "usage": {"inputTokens": 2, "outputTokens": 5,
               "cacheReadTokens": 3, "cacheWriteTokens": 4}},
]
for event in events:
    print(json.dumps(event), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_cli_model_selection_maps_saved_params_and_codex_effort() -> None:
    models = parse_cursor_cli_models(
        "gpt-5.6-sol-high - GPT-5.6 Sol High\ngpt-5.6-sol-max - GPT-5.6 Sol Max\n"
    )
    config = {
        "model_id": "gpt-5.6-sol",
        "model_params": [
            {"id": "reasoning", "value": "max"},
            {"id": "fast", "value": "false"},
        ],
        "follow_codex_effort": True,
    }

    maximum = cli_selection_from_config(
        config, {"reasoning": {"effort": "max"}}, models
    )
    high = cli_selection_from_config(config, {"reasoning": {"effort": "high"}}, models)

    assert maximum.model_id == "gpt-5.6-sol-max"
    assert high.model_id == "gpt-5.6-sol-high"


def test_default_cli_model_follows_codex_model_and_max_effort() -> None:
    models = parse_cursor_cli_models(
        "auto - Auto (GPT-5.6 Sol 272K Low)\n"
        "gpt-5.6-sol-high - GPT-5.6 Sol 1M High\n"
        "gpt-5.6-sol-xhigh - GPT-5.6 Sol 1M Extra High\n"
    )

    selection = cli_selection_from_config(
        {"model_id": "", "follow_codex_effort": True},
        {"model": "gpt-5.6-sol", "reasoning": {"effort": "max"}},
        models,
    )

    assert selection.model_id == "gpt-5.6-sol-xhigh"
    assert selection.display_name == "GPT-5.6 Sol 1M Extra High"
    assert selection.context_window_tokens == 1_000_000
    assert cursor_prompt_byte_limit(selection, 4 * 1_048_576) == 2_000_000


def test_default_cli_model_uses_conservative_272k_budget_without_inference() -> None:
    models = parse_cursor_cli_models("auto - Auto (GPT-5.6 Sol 272K Low)\n")

    selection = cli_selection_from_config(
        {"model_id": "", "follow_codex_effort": True}, {}, models
    )

    assert selection.model_id == ""
    assert selection.context_window_tokens == 272_000
    assert cursor_prompt_byte_limit(selection, 4 * 1_048_576) == 544_000


def test_cursor_cli_uses_agent_default_with_managed_native_tool_denies(
    tmp_path,
) -> None:
    executable = _fake_cursor_cli(tmp_path)
    runner = CursorCliRunner(
        _settings(tmp_path, executable), ConfigStore(tmp_path / "config.json")
    )
    selection = cli_selection_from_config(
        {"model_id": "gpt-5.6-sol-max"},
        {},
        parse_cursor_cli_models("gpt-5.6-sol-max - GPT-5.6 Sol Max\n"),
    )

    command = runner._command(selection, None)
    workspace = runner._workdir()
    permissions_path = workspace / ".cursor" / "cli.json"
    permissions = json.loads(permissions_path.read_text(encoding="utf-8"))

    assert "--mode" not in command
    assert command[command.index("--sandbox") + 1] == "enabled"
    assert "--force" not in command
    assert workspace.name == "bridge-workspace"
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert stat.S_IMODE(permissions_path.stat().st_mode) == 0o600
    assert "Shell(*)" in permissions["permissions"]["deny"]
    assert "Write(/**)" in permissions["permissions"]["deny"]
    assert "Mcp(*:*)" in permissions["permissions"]["deny"]


def test_cursor_cli_streams_deduplicates_usage_and_resumes(tmp_path, caplog) -> None:
    caplog.set_level(
        logging.INFO,
        logger="codex_provider_switchboard.infrastructure.cursor_cli",
    )
    executable = _fake_cursor_cli(tmp_path)
    store = ConfigStore(tmp_path / "config.json")
    store.update_from_api(
        {
            "cursor": {
                "api_key": "test_cursor_key",
                "model_id": "gpt-5.6-sol-max",
            }
        }
    )
    runner = CursorCliRunner(_settings(tmp_path, executable), store)

    async def scenario():
        models = await runner.get_models()
        selection = await runner.effective_selection({})
        first = await runner.create_agent("stdin-only prompt", selection)
        first_events = [event async for event in runner.stream_run(first)]
        usage = await runner.usage(first)
        quota = await runner.quota()
        resumed = await runner.create_run(
            first.agent_id, "stdin-only prompt", selection
        )
        resumed_events = [event async for event in runner.stream_run(resumed)]
        return (
            models,
            selection,
            first,
            first_events,
            usage,
            quota,
            resumed,
            resumed_events,
        )

    (
        models,
        selection,
        first,
        first_events,
        usage,
        quota,
        resumed,
        resumed_events,
    ) = asyncio.run(scenario())
    assert len(models) == 2
    assert selection.model_id == "gpt-5.6-sol-max"
    assert first.reported_model == "GPT-5.6 Sol 272K Max"
    assert [event.data["text"] for event in first_events[:-1]] == ["hel", "lo"]
    assert first_events[-1].data == {"status": "FINISHED", "text": "hello"}
    assert usage == {
        "input_tokens": 9,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens": 5,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 14,
    }
    assert quota["last_run_usage"]["cursor_cli_details"] == {
        "uncached_input_tokens": 2,
        "cache_read_tokens": 3,
        "cache_write_tokens": 4,
    }
    assert resumed.is_continuation is True
    assert resumed.agent_id == first.agent_id
    assert resumed_events[-1].data["status"] == "FINISHED"
    assert "prompt_bytes=" in caplog.text
    assert "first_output_ms=" in caplog.text
    assert "stdin-only prompt" not in caplog.text
    assert "cursor-cli-session" not in caplog.text


def _controllable_cursor_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "cursor-agent-control"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys
import time

if "--list-models" in sys.argv:
    print("test-model - Test Model")
    raise SystemExit(0)
prompt = sys.stdin.read().strip()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "cursor-session", "model": "test-model"}),
      flush=True)
if prompt == "hang":
    time.sleep(60)
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": False, "result": "ok",
                  "session_id": "cursor-session"}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def _configured_cursor_runner(
    tmp_path: Path, executable: Path, *, timeout_seconds: int
) -> CursorCliRunner:
    store = ConfigStore(tmp_path / "control-config.json")
    store.update_from_api(
        {
            "cursor": {
                "api_key": "test_cursor_key",
                "model_id": "test-model",
            }
        }
    )
    runner = CursorCliRunner(_settings(tmp_path, executable), store)
    # ConfigStore enforces a production minimum; use a short unit-test deadline.
    runner._connection = lambda: ("test_cursor_key", timeout_seconds)  # type: ignore[method-assign]
    return runner


def test_cursor_cancellation_releases_concurrency_slot(tmp_path) -> None:
    executable = _controllable_cursor_cli(tmp_path)
    runner = _configured_cursor_runner(tmp_path, executable, timeout_seconds=5)

    async def scenario() -> str:
        selection = await runner.effective_selection({})
        first = await runner.create_agent("hang", selection)

        async def consume_first() -> None:
            _ = [event async for event in runner.stream_run(first)]

        task = asyncio.create_task(consume_first())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        second = await asyncio.wait_for(runner.create_agent("ok", selection), timeout=2)
        events = [event async for event in runner.stream_run(second)]
        return events[-1].data["text"]

    assert asyncio.run(scenario()) == "ok"


def test_cursor_timeout_kills_process_and_releases_slot(tmp_path) -> None:
    executable = _controllable_cursor_cli(tmp_path)
    runner = _configured_cursor_runner(tmp_path, executable, timeout_seconds=1)

    async def scenario() -> str:
        selection = await runner.effective_selection({})
        first = await runner.create_agent("hang", selection)
        with pytest.raises(CursorCliError, match="timed out"):
            _ = [event async for event in runner.stream_run(first)]
        second = await asyncio.wait_for(runner.create_agent("ok", selection), timeout=2)
        events = [event async for event in runner.stream_run(second)]
        return events[-1].data["text"]

    assert asyncio.run(scenario()) == "ok"


def test_cursor_first_output_timeout_is_explicit_and_releases_slot(tmp_path) -> None:
    executable = _controllable_cursor_cli(tmp_path)
    runner = _configured_cursor_runner(tmp_path, executable, timeout_seconds=5)
    runner.settings = replace(runner.settings, cursor_first_output_timeout_seconds=0.2)

    async def scenario() -> str:
        selection = await runner.effective_selection({})
        first = await runner.create_agent("hang", selection)
        with pytest.raises(CursorCliError, match="no assistant output") as caught:
            _ = [event async for event in runner.stream_run(first)]
        assert caught.value.status_code == 400
        second = await asyncio.wait_for(runner.create_agent("ok", selection), timeout=2)
        events = [event async for event in runner.stream_run(second)]
        return events[-1].data["text"]

    assert asyncio.run(scenario()) == "ok"
