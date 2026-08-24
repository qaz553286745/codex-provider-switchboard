from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any


def isolated_subprocess_kwargs() -> dict[str, Any]:
    """Return options that place a CLI tree in an isolated process group."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


def _linux_process_group_has_live_members(
    process_group_id: int,
    *,
    proc: Path = Path("/proc"),
) -> bool | None:
    """Return whether a Linux process group contains a non-zombie member.

    ``killpg(pgid, 0)`` reports success while a killed process is waiting to be
    reaped.  Those zombie-only groups have already stopped executing and must
    not keep shutdown waiting.  ``None`` asks the caller to retain the portable
    POSIX result when procfs is unavailable or cannot be inspected.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None

    matched_group = False
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # /proc/<pid>/stat wraps the command name in parentheses.  Split after
        # the final closing parenthesis because the name itself may contain
        # spaces or parentheses.  The following fields are state, ppid, pgrp.
        closing = raw.rfind(")")
        if closing < 0:
            continue
        fields = raw[closing + 1 :].split()
        if len(fields) < 3:
            continue
        try:
            member_group = int(fields[2])
        except ValueError:
            continue
        if member_group == process_group_id:
            matched_group = True
            if fields[0] != "Z":
                return True

    return False if matched_group else None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    linux_live_members = _linux_process_group_has_live_members(process_group_id)
    return True if linux_live_members is None else linux_live_members


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
