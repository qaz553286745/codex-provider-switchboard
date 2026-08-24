from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from codex_provider_switchboard.infrastructure.process_control import (
    _linux_process_group_has_live_members,
    isolated_subprocess_kwargs,
    terminate_process_tree,
)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        closing = raw.rfind(")")
        if closing >= 0:
            fields = raw[closing + 1 :].split()
            if fields and fields[0] == "Z":
                return False
    return True


async def _wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def test_linux_process_group_ignores_zombie_members(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    zombie = tmp_path / "101"
    zombie.mkdir()
    (zombie / "stat").write_text(
        "101 (terminated worker) Z 1 42 42 0 0 0\n", encoding="utf-8"
    )

    assert not _linux_process_group_has_live_members(42, proc=tmp_path)

    live = tmp_path / "102"
    live.mkdir()
    (live / "stat").write_text(
        "102 (active worker) S 1 42 42 0 0 0\n", encoding="utf-8"
    )

    assert _linux_process_group_has_live_members(42, proc=tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups required")
def test_terminate_process_tree_kills_term_resistant_grandchild(tmp_path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    child_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent_code = (
        "import pathlib,signal,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid));"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )

    async def scenario() -> int:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_code,
            str(pid_path),
            child_code,
            **isolated_subprocess_kwargs(),
        )
        await _wait_for_file(pid_path)
        grandchild_pid = int(pid_path.read_text())
        assert _process_exists(grandchild_pid)
        await terminate_process_tree(process, grace_seconds=0.1)
        for _ in range(100):
            if not _process_exists(grandchild_pid):
                break
            await asyncio.sleep(0.02)
        assert process.returncode is not None
        return grandchild_pid

    grandchild_pid = asyncio.run(scenario())
    assert not _process_exists(grandchild_pid)
