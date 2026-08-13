from __future__ import annotations

import asyncio
import os
import signal
from typing import Any


def isolated_subprocess_kwargs() -> dict[str, Any]:
    """Return options that place a CLI tree in an isolated process group."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_group_exit(
    process_group_id: int, timeout_seconds: float
) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
    while _process_group_exists(process_group_id):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


async def terminate_process_tree(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 2.0
) -> None:
    """Terminate a managed CLI and all descendants in its isolated group."""
    grace_seconds = max(0.0, grace_seconds)
    if os.name != "posix":
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
        return

    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            await process.wait()
        return
    except PermissionError:
        if process.returncode is None:
            process.terminate()

    exited = await _wait_for_process_group_exit(process_group_id, grace_seconds)
    if not exited:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.returncode is None:
                process.kill()
    if process.returncode is None:
        await process.wait()
