from __future__ import annotations

import asyncio
import contextlib
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from codex_provider_switchboard.infrastructure.kiro_cli import (
    KiroInvocationError,
    KiroRunner,
    parse_kiro_usage,
)
from codex_provider_switchboard.settings import AppSettings


def _settings() -> AppSettings:
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
        kiro_workdir=Path("/tmp/switchboard-test"),
        kiro_timeout_seconds=30,
        kiro_max_concurrency=1,
        kiro_max_prompt_bytes=1_048_576,
        kiro_context_recovery_prompt_bytes=512 * 1_024,
        kiro_max_output_bytes=1_048_576,
        kiro_allow_requested_model=False,
        kiro_tool_batching=True,
        cursor_cli="cursor-agent",
        cursor_workdir=Path("/tmp/switchboard-cursor-test"),
        cursor_max_concurrency=1,
        cursor_max_prompt_bytes=1_048_576,
        cursor_max_output_bytes=1_048_576,
    )


def test_command_forwards_max_effort_and_explicit_session() -> None:
    runner = KiroRunner(_settings())
    command = runner.command("gpt-5.6-sol", "max", resume_id="kiro-session-id")
    assert command[command.index("--effort") + 1] == "max"
    assert command[command.index("--resume-id") + 1] == "kiro-session-id"
    assert "--resume" not in command


def test_command_can_resume_latest_and_omit_effort() -> None:
    runner = KiroRunner(_settings())
    command = runner.command("gpt-5.6-sol", None, resume_latest=True)
    assert "--resume" in command
    assert "--effort" not in command


def test_parse_kiro_usage_extracts_remaining_credits() -> None:
    usage = parse_kiro_usage(
        "Estimated Usage | resets on 2026-08-01 | KIRO PRO+\n"
        "Credits (637.54 of 2000 covered in plan)\n31%\n"
    )
    assert usage["used"] == 637.54
    assert usage["total"] == 2000
    assert usage["remaining"] == 1362.46
    assert usage["plan"] == "KIRO PRO+"


def _controllable_kiro_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "kiro-cli"
    executable.write_text(
        """#!/usr/bin/env python3
import sys
import time

prompt = sys.stdin.read()
if prompt == "hang":
    time.sleep(60)
print("ok", flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_kiro_cancellation_releases_generation_capacity(tmp_path) -> None:
    executable = _controllable_kiro_cli(tmp_path)
    settings = replace(
        _settings(),
        kiro_cli=str(executable),
        kiro_workdir=tmp_path / "kiro",
        kiro_timeout_seconds=5,
    )
    runner = KiroRunner(settings)

    async def scenario() -> str:
        task = asyncio.create_task(runner.generate("hang", "model", None))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return await asyncio.wait_for(runner.generate("ok", "model", None), timeout=2)

    assert asyncio.run(scenario()) == "ok"


def test_kiro_timeout_kills_process_and_releases_capacity(tmp_path) -> None:
    executable = _controllable_kiro_cli(tmp_path)
    settings = replace(
        _settings(),
        kiro_cli=str(executable),
        kiro_workdir=tmp_path / "kiro",
        kiro_timeout_seconds=0.1,
    )
    runner = KiroRunner(settings)

    async def scenario() -> str:
        with pytest.raises(KiroInvocationError, match="timed out"):
            await runner.generate("hang", "model", None)
        return await asyncio.wait_for(runner.generate("ok", "model", None), timeout=2)

    assert asyncio.run(scenario()) == "ok"
