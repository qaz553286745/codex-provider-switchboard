from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from codex_provider_switchboard.infrastructure.process_control import (
    isolated_subprocess_kwargs,
    terminate_process_tree,
)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


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
