from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import pty
import re
import select
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..domain.bridge import clean_kiro_stdout
from ..settings import AppSettings
from .process_control import isolated_subprocess_kwargs, terminate_process_tree
from .scheduler import CapacityTimeoutError, FairCapacityScheduler

logger = logging.getLogger(__name__)


class KiroInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout_bytes: int = 0,
        terminal: bool = False,
        busy: bool = False,
    ) -> None:
        super().__init__(message)
        self.stdout_bytes = stdout_bytes
        self.terminal = terminal
        self.busy = busy


def _decimal(value: str) -> float:
    return float(value.replace(",", ""))


def parse_kiro_usage(output: str) -> dict[str, Any]:
    """Parse the stable, user-visible fields printed by Kiro CLI `/usage`."""
    cleaned = clean_kiro_stdout(output).replace("\r", "")
    heading = re.search(
        r"Estimated Usage\s*\|\s*resets on\s*([^|\n]+)\|\s*([^\n]+)",
        cleaned,
        re.IGNORECASE,
    )
    credits = re.search(
        r"Credits\s*\(\s*([\d,.]+)\s+of\s+([\d,.]+)\s+covered in plan\s*\)",
        cleaned,
        re.IGNORECASE,
    )
    if credits is None:
        raise KiroInvocationError("Kiro CLI /usage returned an unknown format.")
    used = _decimal(credits.group(1))
    total = _decimal(credits.group(2))
    result: dict[str, Any] = {
        "status": "available",
        "source": "kiro-cli /usage",
        "used": used,
        "total": total,
        "remaining": max(total - used, 0.0),
        "unit": "credits",
    }
    if total > 0:
        result["percent_used"] = used / total * 100
    if heading is not None:
        result["reset_at"] = heading.group(1).strip()[:100]
        result["plan"] = heading.group(2).strip()[:100]
    return result


class KiroRunner:
    """Run Kiro CLI with prompts on stdin and bounded, streamed stdout."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._generation_scheduler = FairCapacityScheduler(
            settings.kiro_max_concurrency,
            queue_timeout_seconds=settings.kiro_queue_timeout_seconds,
        )
        self._control_scheduler = FairCapacityScheduler(
            1,
            queue_timeout_seconds=min(settings.kiro_queue_timeout_seconds, 30),
        )
        self._models_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._models_lock = asyncio.Lock()
        self._usage_cache: tuple[float, dict[str, Any]] | None = None
        self._usage_lock = asyncio.Lock()

    @staticmethod
    async def _read_stderr_tail(
        stream: asyncio.StreamReader, limit: int = 131_072
    ) -> bytes:
        tail = bytearray()
        while chunk := await stream.read(4_096):
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[: len(tail) - limit]
        return bytes(tail)

    def command(
        self,
        model: str,
        effort: str | None,
        *,
        resume_id: str | None = None,
        resume_latest: bool = False,
    ) -> list[str]:
        if not model or len(model) > 200 or any(ord(char) < 0x20 for char in model):
            raise KiroInvocationError("Kiro model must be a printable identifier.")
        if effort not in {None, "low", "medium", "high", "xhigh", "max"}:
            raise KiroInvocationError("Unsupported Kiro reasoning effort.")
        if resume_id is not None and (
            len(resume_id) > 500 or any(ord(char) < 0x20 for char in resume_id)
        ):
            raise KiroInvocationError("Invalid Kiro session identifier.")

        command = [
            self.settings.kiro_cli,
            "chat",
            "--no-interactive",
            "--model",
            model,
        ]
        if effort is not None:
            command.extend(["--effort", effort])
        if resume_id is not None:
            command.extend(["--resume-id", resume_id])
        elif resume_latest:
            command.append("--resume")
        command.extend(["--wrap", "never"])
        return command

    async def stream(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        *,
        workdir: Path | None = None,
        resume_id: str | None = None,
        resume_latest: bool = False,
    ) -> AsyncIterator[str]:
        encoded = prompt.encode("utf-8")
        if len(encoded) > self.settings.kiro_max_prompt_bytes:
            raise KiroInvocationError(
                f"Rendered prompt is {len(encoded)} bytes; limit is "
                f"{self.settings.kiro_max_prompt_bytes}.",
                terminal=True,
            )

        target_workdir = workdir or self.settings.kiro_workdir
        workdir_existed = target_workdir.exists()
        target_workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not workdir_existed:
            os.chmod(target_workdir, 0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "NO_COLOR": "1",
                "CLICOLOR": "0",
                "FORCE_COLOR": "0",
                "TERM": "dumb",
            }
        )
        command = self.command(
            model,
            effort,
            resume_id=resume_id,
            resume_latest=resume_latest,
        )

        invocation_started = time.monotonic()
        try:
            execution_lease = await self._generation_scheduler.acquire()
        except CapacityTimeoutError as exc:
            raise KiroInvocationError(
                "Kiro generation capacity is busy; retry later.",
                busy=True,
            ) from exc
        semaphore_acquired = invocation_started + execution_lease.queue_ms / 1_000
        async with execution_lease:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=target_workdir,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **isolated_subprocess_kwargs(),
                )
                process_started = time.monotonic()
            except FileNotFoundError as exc:
                raise KiroInvocationError(
                    f"Kiro CLI was not found: {self.settings.kiro_cli}"
                ) from exc
            except OSError as exc:
                raise KiroInvocationError(
                    f"Kiro CLI could not start: {type(exc).__name__}."
                ) from exc

            stdin = process.stdin
            stdout = process.stdout
            stderr = process.stderr
            if stdin is None or stdout is None or stderr is None:
                await terminate_process_tree(process)
                raise KiroInvocationError("Kiro CLI pipes were unavailable.")
            stderr_task = asyncio.create_task(
                self._read_stderr_tail(stderr), name="kiro-stderr"
            )
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            deadline = (
                asyncio.get_running_loop().time() + self.settings.kiro_timeout_seconds
            )
            stdout_bytes = 0
            first_stdout_at: float | None = None

            def remaining() -> float:
                value = deadline - asyncio.get_running_loop().time()
                if value <= 0:
                    raise TimeoutError
                return value

            try:
                try:
                    stdin.write(encoded)
                    await asyncio.wait_for(stdin.drain(), timeout=remaining())
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stdin.close()

                while True:
                    data = await asyncio.wait_for(
                        stdout.read(4_096), timeout=remaining()
                    )
                    if not data:
                        break
                    if first_stdout_at is None:
                        first_stdout_at = time.monotonic()
                    stdout_bytes += len(data)
                    if stdout_bytes > self.settings.kiro_max_output_bytes:
                        raise KiroInvocationError(
                            "Kiro CLI output exceeded the configured byte limit.",
                            stdout_bytes=stdout_bytes,
                        )
                    chunk = decoder.decode(data)
                    if chunk:
                        yield chunk

                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                await asyncio.wait_for(process.wait(), timeout=remaining())
                stderr = await asyncio.wait_for(stderr_task, timeout=remaining())
            except TimeoutError as exc:
                await terminate_process_tree(process)
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise KiroInvocationError(
                    f"Kiro CLI timed out after "
                    f"{self.settings.kiro_timeout_seconds:g}s.",
                    stdout_bytes=stdout_bytes,
                ) from exc
            except (asyncio.CancelledError, GeneratorExit):
                logger.info(
                    "Kiro CLI invocation cancelled duration_ms=%d stdout_bytes=%d",
                    int((time.monotonic() - invocation_started) * 1_000),
                    stdout_bytes,
                )
                await terminate_process_tree(process)
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise
            except KiroInvocationError:
                await terminate_process_tree(process)
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise
            except Exception as exc:
                await terminate_process_tree(process)
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise KiroInvocationError(
                    f"Kiro CLI stream failed: {type(exc).__name__}.",
                    stdout_bytes=stdout_bytes,
                ) from exc

        completed_at = time.monotonic()
        logger.info(
            "Kiro CLI invocation completed queue_ms=%d startup_ms=%d "
            "first_stdout_ms=%s duration_ms=%d stdout_bytes=%d",
            int((semaphore_acquired - invocation_started) * 1_000),
            int((process_started - semaphore_acquired) * 1_000),
            (
                str(int((first_stdout_at - process_started) * 1_000))
                if first_stdout_at is not None
                else "none"
            ),
            int((completed_at - invocation_started) * 1_000),
            stdout_bytes,
        )
        stderr_text = clean_kiro_stdout(stderr.decode("utf-8", errors="replace"))
        if process.returncode != 0:
            detail_available = bool(stderr_text)
            raise KiroInvocationError(
                f"Kiro CLI exited with code {process.returncode}. "
                f"Stderr detail available: {detail_available}.",
                stdout_bytes=stdout_bytes,
            )
        if stdout_bytes == 0:
            raise KiroInvocationError("Kiro CLI returned an empty stdout response.")

    async def generate(
        self,
        prompt: str,
        model: str,
        effort: str | None,
        **kwargs: Any,
    ) -> str:
        parts = [chunk async for chunk in self.stream(prompt, model, effort, **kwargs)]
        cleaned = clean_kiro_stdout("".join(parts))
        if not cleaned:
            raise KiroInvocationError("Kiro CLI returned an empty stdout response.")
        return cleaned

    async def latest_session_id(self, workdir: Path) -> str | None:
        """Return the newest Kiro session for one isolated Codex work directory."""
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(workdir, 0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "NO_COLOR": "1",
                "CLICOLOR": "0",
                "FORCE_COLOR": "0",
                "TERM": "dumb",
            }
        )
        command = [
            self.settings.kiro_cli,
            "chat",
            "--list-sessions",
            "--format",
            "json",
        ]

        process: asyncio.subprocess.Process | None = None
        try:
            try:
                control_lease = await self._control_scheduler.acquire()
            except CapacityTimeoutError:
                logger.warning("Kiro session discovery queue deadline exceeded")
                return None
            async with control_lease:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=workdir,
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    **isolated_subprocess_kwargs(),
                )
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(self.settings.kiro_timeout_seconds, 30),
                )
        except TimeoutError:
            if process is not None:
                await terminate_process_tree(process)
            return None
        except asyncio.CancelledError:
            if process is not None:
                await terminate_process_tree(process)
            raise
        except (FileNotFoundError, OSError):
            return None
        if process is None:
            return None
        if process.returncode != 0 or len(stdout) > self.settings.kiro_max_output_bytes:
            return None

        try:
            groups = json.loads(
                clean_kiro_stdout(stdout.decode("utf-8", errors="replace"))
            )
        except json.JSONDecodeError:
            return None
        if not isinstance(groups, list):
            return None

        target = os.path.realpath(workdir)
        matching: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(
                group.get("sessions"), list
            ):
                continue
            if (
                isinstance(group.get("cwd"), str)
                and os.path.realpath(group["cwd"]) == target
            ):
                matching.extend(
                    item for item in group["sessions"] if isinstance(item, dict)
                )

        candidates = [
            item for item in matching if isinstance(item.get("sessionId"), str)
        ]
        if not candidates:
            return None
        newest = max(candidates, key=lambda item: str(item.get("updatedAt") or ""))
        return str(newest["sessionId"])

    def _control_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "NO_COLOR": "1",
                "CLICOLOR": "0",
                "FORCE_COLOR": "0",
                "TERM": "dumb",
            }
        )
        return environment

    async def list_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._models_cache
        if not force and cached and now - cached[0] < 300:
            return [dict(item) for item in cached[1]]

        async with self._models_lock:
            now = time.monotonic()
            cached = self._models_cache
            if not force and cached and now - cached[0] < 300:
                return [dict(item) for item in cached[1]]
            command = [
                self.settings.kiro_cli,
                "chat",
                "--list-models",
                "--format",
                "json",
            ]
            self.settings.kiro_workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.settings.kiro_workdir,
                    env=self._control_environment(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **isolated_subprocess_kwargs(),
                )
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(self.settings.kiro_timeout_seconds, 30),
                )
            except FileNotFoundError as exc:
                raise KiroInvocationError(
                    f"Kiro CLI was not found: {self.settings.kiro_cli}"
                ) from exc
            except TimeoutError as exc:
                if process is not None:
                    await terminate_process_tree(process)
                raise KiroInvocationError("Kiro model catalog timed out.") from exc
            except OSError as exc:
                raise KiroInvocationError(
                    f"Kiro model catalog failed: {type(exc).__name__}."
                ) from exc
            if process.returncode != 0:
                raise KiroInvocationError(
                    f"Kiro model catalog exited with code {process.returncode}."
                )
            if len(stdout) > min(self.settings.kiro_max_output_bytes, 4 * 1_048_576):
                raise KiroInvocationError("Kiro model catalog exceeded the byte limit.")
            try:
                payload = json.loads(
                    clean_kiro_stdout(stdout.decode("utf-8", errors="replace"))
                )
            except json.JSONDecodeError as exc:
                raise KiroInvocationError(
                    "Kiro model catalog returned invalid JSON."
                ) from exc
            raw_models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(raw_models, list):
                raise KiroInvocationError("Kiro model catalog did not contain models.")
            models = [item for item in raw_models if isinstance(item, dict)][:1_000]
            if not models:
                raise KiroInvocationError("Kiro model catalog was empty.")
            self._models_cache = (time.monotonic(), models)
            return [dict(item) for item in models]

    @staticmethod
    def _usage_exchange(master_fd: int, timeout: float, byte_limit: int) -> str:
        deadline = time.monotonic() + timeout
        raw = bytearray()
        dsr_replies = 0
        usage_sent = False
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if not readable:
                continue
            try:
                chunk = os.read(master_fd, 4_096)
            except OSError:
                break
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > byte_limit:
                raise KiroInvocationError("Kiro /usage output exceeded the byte limit.")

            query_count = raw.count(b"\x1b[6n")
            while dsr_replies < query_count:
                os.write(master_fd, b"\x1b[1;1R")
                dsr_replies += 1

            text = clean_kiro_stdout(raw.decode("utf-8", errors="replace"))
            if not usage_sent and "Plan:" in text and "/usage" in text:
                os.write(master_fd, b"/usage\r")
                usage_sent = True
            if usage_sent and "Estimated Usage" in text and "covered in plan)" in text:
                return text
        raise KiroInvocationError(
            "Kiro CLI /usage timed out or returned no quota data."
        )

    async def usage(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._usage_cache
        if not force and cached and now - cached[0] < 60:
            return dict(cached[1])

        async with self._usage_lock:
            now = time.monotonic()
            cached = self._usage_cache
            if not force and cached and now - cached[0] < 60:
                return dict(cached[1])

            workdir = self.settings.kiro_workdir / "control"
            workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(workdir, 0o700)
            master_fd, slave_fd = pty.openpty()
            process: asyncio.subprocess.Process | None = None
            timeout = min(self.settings.kiro_timeout_seconds, 45)
            try:
                process = await asyncio.create_subprocess_exec(
                    self.settings.kiro_cli,
                    "chat",
                    "--legacy-ui",
                    cwd=workdir,
                    env=self._control_environment(),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    **isolated_subprocess_kwargs(),
                )
                os.close(slave_fd)
                slave_fd = -1
                output = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._usage_exchange,
                        master_fd,
                        timeout,
                        min(self.settings.kiro_max_output_bytes, 4 * 1_048_576),
                    ),
                    timeout=timeout + 2,
                )
                parsed = parse_kiro_usage(output)
            except FileNotFoundError as exc:
                raise KiroInvocationError(
                    f"Kiro CLI was not found: {self.settings.kiro_cli}"
                ) from exc
            except TimeoutError as exc:
                raise KiroInvocationError("Kiro CLI /usage timed out.") from exc
            finally:
                if slave_fd >= 0:
                    os.close(slave_fd)
                if process is not None:
                    await terminate_process_tree(process)
                os.close(master_fd)
            self._usage_cache = (time.monotonic(), parsed)
            return dict(parsed)
