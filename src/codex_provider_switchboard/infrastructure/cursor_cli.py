from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings import AppSettings
from .config_store import ConfigStore
from .cursor_client import (
    CursorBackendError,
    CursorModelSelection,
    CursorRun,
    CursorStreamEvent,
    requested_effort,
)
from .process_control import isolated_subprocess_kwargs, terminate_process_tree

_MAX_CATALOG_BYTES = 4 * 1_048_576
_MAX_NDJSON_LINE_BYTES = 8 * 1_048_576
_MAX_STDERR_BYTES = 131_072
_DEFAULT_CONTEXT_WINDOW_TOKENS = 272_000
_SAFE_PROMPT_BYTES_PER_CONTEXT_TOKEN = 2
CURSOR_CLI_ENDPOINT = "https://api2.cursor.sh"
CURSOR_CLI_SESSION_PROFILE = "agent-delegation-v1"
_MODEL_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,199})\s+-\s+(.{1,500})$")
_CONTEXT_WINDOW_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([KM])\b", re.IGNORECASE
)
_EFFORT_SUFFIXES = ("extra-high", "medium", "xhigh", "high", "none", "low", "max")

logger = logging.getLogger(__name__)

_BRIDGE_CLI_CONFIG = {
    "permissions": {
        "allow": [],
        "deny": [
            "Shell(*)",
            "Read(*)",
            "Read(**)",
            "Read(/**)",
            "Write(*)",
            "Write(**)",
            "Write(/**)",
            "Mcp(*)",
            "Mcp(*:*)",
        ],
    }
}


class CursorCliError(CursorBackendError):
    """Failure while invoking the local Cursor Agent CLI."""


class _CursorFirstOutputTimeout(TimeoutError):
    """The CLI initialized but never emitted assistant or terminal output."""


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == encoded:
            os.chmod(path, 0o600)
            return
    except OSError:
        pass

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _context_window_tokens(display_name: str) -> int | None:
    matches = list(_CONTEXT_WINDOW_RE.finditer(display_name))
    if not matches:
        return None
    value = float(matches[-1].group(1))
    multiplier = 1_000 if matches[-1].group(2).upper() == "K" else 1_000_000
    tokens = int(value * multiplier)
    return tokens if 1_000 <= tokens <= 10_000_000 else None


def cursor_prompt_byte_limit(
    selection: CursorModelSelection, configured_limit: int
) -> int:
    """Return a conservative byte budget for the selected Cursor context window.

    Cursor exposes context capacity in model display names but does not expose a
    tokenizer through the CLI. Two UTF-8 bytes per advertised context token keeps
    room for tokenization variance and the model's response while still allowing
    substantially larger prompts on 1M variants.
    """
    context_tokens = selection.context_window_tokens or _DEFAULT_CONTEXT_WINDOW_TOKENS
    model_limit = context_tokens * _SAFE_PROMPT_BYTES_PER_CONTEXT_TOKEN
    return min(configured_limit, model_limit)


def parse_cursor_cli_models(output: str) -> list[dict[str, Any]]:
    """Parse the bounded, line-oriented catalog printed by cursor-agent."""
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in output.replace("\r", "").splitlines():
        match = _MODEL_LINE_RE.fullmatch(raw_line.strip())
        if match is None or match.group(1) in seen:
            continue
        model_id, display_name = match.groups()
        display_name = display_name.strip()
        model: dict[str, Any] = {
            "id": model_id,
            "displayName": display_name,
            "description": "Available through the local Cursor Agent CLI.",
        }
        context_tokens = _context_window_tokens(display_name)
        if context_tokens is not None:
            model["contextWindowTokens"] = context_tokens
        models.append(model)
        seen.add(model_id)
        if len(models) >= 1_000:
            break
    if not models:
        raise CursorCliError("Cursor CLI model catalog was empty or invalid.")
    return models


def _normalized_effort(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("_", "-")
    return {
        "extra-high": "xhigh",
        "ultra": "max",
        "minimal": "low",
    }.get(normalized, normalized)


def _effort_candidates(value: str) -> tuple[str, ...]:
    return {
        "none": ("none", "low"),
        "low": ("low", "none"),
        "medium": ("medium",),
        "high": ("high",),
        "xhigh": ("xhigh", "extra-high", "max", "high"),
        "max": ("max", "xhigh", "extra-high", "high"),
    }.get(value, (value,))


def _split_cli_model(model_id: str) -> tuple[str, str | None, bool]:
    fast = model_id.endswith("-fast")
    value = model_id[:-5] if fast else model_id
    for suffix in _EFFORT_SUFFIXES:
        marker = f"-{suffix}"
        if value.endswith(marker) and len(value) > len(marker):
            return value[: -len(marker)], _normalized_effort(suffix), fast
    return value, None, fast


def _configured_parameters(config: dict[str, Any]) -> tuple[str | None, bool | None]:
    effort: str | None = None
    fast: bool | None = None
    raw_params = config.get("model_params")
    if not isinstance(raw_params, list):
        return effort, fast
    for item in raw_params:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        compact = re.sub(r"[^a-z0-9]", "", item["id"].lower())
        value = item.get("value")
        if "effort" in compact or compact in {
            "reasoning",
            "reasoninglevel",
            "thinkingeffort",
        }:
            effort = _normalized_effort(value)
        elif compact in {"fast", "fastmode", "usefast"}:
            if isinstance(value, bool):
                fast = value
            elif isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    fast = True
                elif normalized in {"0", "false", "no", "off"}:
                    fast = False
    return effort, fast


def cli_selection_from_config(
    cursor_config: dict[str, Any],
    body: dict[str, Any],
    models: list[dict[str, Any]],
) -> CursorModelSelection:
    """Resolve Cloud-style saved parameters to a concrete CLI model ID."""
    configured_id = str(cursor_config.get("model_id") or "").strip()
    catalog = {
        str(item["id"]): item
        for item in models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    requested = requested_effort(body)
    infer_from_codex = (
        configured_id in {"", "auto"}
        and cursor_config.get("follow_codex_effort") is True
        and requested is not None
    )
    if infer_from_codex:
        requested_model = body.get("model")
        if isinstance(requested_model, str) and requested_model.strip():
            base_id, _, _ = _split_cli_model(requested_model.strip())
        else:
            base_id = ""
    else:
        base_id, _, _ = _split_cli_model(configured_id)

    if not base_id:
        return CursorModelSelection(
            "", (), "Cursor CLI default", _DEFAULT_CONTEXT_WINDOW_TOKENS
        )

    _, id_effort, id_fast = _split_cli_model(configured_id or base_id)
    param_effort, param_fast = _configured_parameters(cursor_config)
    desired_effort = param_effort or id_effort
    if cursor_config.get("follow_codex_effort") is True and requested is not None:
        desired_effort = _normalized_effort(requested)
    desired_fast = id_fast if param_fast is None else param_fast

    candidates: list[str] = []
    if desired_effort is not None:
        for effort in _effort_candidates(desired_effort):
            candidate = f"{base_id}-{effort}"
            if desired_fast:
                candidate += "-fast"
            candidates.append(candidate)
    if configured_id:
        candidates.append(configured_id)
    candidates.append(base_id)

    for candidate in candidates:
        model = catalog.get(candidate)
        if model is not None:
            display_name = str(model.get("displayName") or candidate)
            context_tokens = model.get("contextWindowTokens")
            if not isinstance(context_tokens, int):
                context_tokens = _context_window_tokens(display_name)
            return CursorModelSelection(candidate, (), display_name, context_tokens)
    if infer_from_codex:
        return CursorModelSelection(
            "", (), "Cursor CLI default", _DEFAULT_CONTEXT_WINDOW_TOKENS
        )
    raise CursorCliError(
        "Configured Cursor CLI model is unavailable. Refresh the official model "
        "catalog and select an installed model."
    )


def _usage_from_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    def token(name: str) -> int:
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0
        return max(0, int(raw))

    uncached = token("inputTokens")
    output = token("outputTokens")
    cache_read = token("cacheReadTokens")
    cache_write = token("cacheWriteTokens")
    input_total = uncached + cache_read + cache_write
    return {
        "input_tokens": input_total,
        "input_tokens_details": {"cached_tokens": cache_read},
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_total + output,
        "cursor_cli_details": {
            "uncached_input_tokens": uncached,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
        },
    }


@dataclass(slots=True)
class _CliProcessState:
    process: asyncio.subprocess.Process
    stdout: asyncio.StreamReader
    stderr_task: asyncio.Task[bytes]
    deadline: float
    started_at: float
    first_output_deadline: float
    prompt_bytes: int
    prompt_limit_bytes: int
    selected_model: str
    resumed: bool
    stdout_bytes: int = 0
    pending_assistant: str | None = None
    emitted_text: str = ""
    terminal_seen: bool = False
    usage: dict[str, Any] | None = None
    initialized_at: float | None = None
    first_output_at: float | None = None
    outcome_logged: bool = False
    closed: bool = False


class CursorCliRunner:
    """Run Cursor Agent locally with stdin prompts and bounded NDJSON output."""

    backend_id = "cli"
    runtime_name = "Cursor Agent CLI"
    session_name = "Cursor CLI session"

    def __init__(self, settings: AppSettings, store: ConfigStore) -> None:
        self.settings = settings
        self.store = store
        self._semaphore = asyncio.Semaphore(settings.cursor_max_concurrency)
        self._models_cache: tuple[str, float, list[dict[str, Any]]] | None = None
        self._models_lock = asyncio.Lock()
        self._last_usage: dict[str, Any] | None = None

    def _connection(self) -> tuple[str, int]:
        config = self.store.read()["cursor"]
        api_key = self.store.api_key()
        if not api_key:
            raise CursorCliError(
                "Cursor API key is not configured. Open the local control panel first."
            )
        return api_key, int(config["timeout_seconds"])

    def _environment(self, api_key: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("CURSOR_API_ENDPOINT", None)
        environment.update(
            {
                "CURSOR_API_KEY": api_key,
                "NO_COLOR": "1",
                "CLICOLOR": "0",
                "FORCE_COLOR": "0",
                "TERM": "dumb",
            }
        )
        return environment

    def _workdir(self) -> Path:
        root = self.settings.cursor_workdir
        existed = root.exists()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed:
            os.chmod(root, 0o700)

        workspace = root / "bridge-workspace"
        cursor_directory = workspace / ".cursor"
        try:
            workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            cursor_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for directory in (workspace, cursor_directory):
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise CursorCliError(
                        "Cursor bridge workspace contains an unsafe directory."
                    )
                os.chmod(directory, 0o700)
            _atomic_private_json(cursor_directory / "cli.json", _BRIDGE_CLI_CONFIG)
        except CursorCliError:
            raise
        except OSError as exc:
            raise CursorCliError(
                f"Cursor bridge workspace setup failed: {type(exc).__name__}."
            ) from exc
        return workspace

    @staticmethod
    async def _read_stderr_tail(stream: asyncio.StreamReader) -> bytes:
        tail = bytearray()
        while chunk := await stream.read(4_096):
            tail.extend(chunk)
            if len(tail) > _MAX_STDERR_BYTES:
                del tail[: len(tail) - _MAX_STDERR_BYTES]
        return bytes(tail)

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
        raw = bytearray()
        while chunk := await stream.read(4_096):
            raw.extend(chunk)
            if len(raw) > limit:
                raise CursorCliError("Cursor CLI output exceeded the byte limit.")
        return bytes(raw)

    @staticmethod
    def _remaining(state: _CliProcessState) -> float:
        remaining = state.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    async def _read_event(self, state: _CliProcessState) -> dict[str, Any] | None:
        timeout = self._remaining(state)
        if state.first_output_at is None:
            first_output_remaining = (
                state.first_output_deadline - asyncio.get_running_loop().time()
            )
            if first_output_remaining <= 0:
                raise _CursorFirstOutputTimeout
            timeout = min(timeout, first_output_remaining)
        try:
            line = await asyncio.wait_for(state.stdout.readline(), timeout=timeout)
        except TimeoutError:
            if (
                state.first_output_at is None
                and asyncio.get_running_loop().time() >= state.first_output_deadline
            ):
                raise _CursorFirstOutputTimeout from None
            raise
        except ValueError as exc:
            raise CursorCliError(
                "Cursor CLI emitted an oversized NDJSON line."
            ) from exc
        if not line:
            return None
        state.stdout_bytes += len(line)
        if state.stdout_bytes > self.settings.cursor_max_output_bytes:
            raise CursorCliError(
                "Cursor CLI output exceeded the configured byte limit."
            )
        if len(line) > _MAX_NDJSON_LINE_BYTES:
            raise CursorCliError("Cursor CLI emitted an oversized NDJSON line.")
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CursorCliError("Cursor CLI returned invalid stream JSON.") from exc
        if not isinstance(value, dict):
            raise CursorCliError("Cursor CLI returned an unexpected stream event.")
        return value

    @staticmethod
    def _record_first_output(state: _CliProcessState) -> None:
        if state.first_output_at is None:
            state.first_output_at = asyncio.get_running_loop().time()

    @staticmethod
    def _log_outcome(state: _CliProcessState, outcome: str) -> None:
        if state.outcome_logged:
            return
        state.outcome_logged = True
        now = asyncio.get_running_loop().time()
        first_output_ms = (
            None
            if state.first_output_at is None
            else round((state.first_output_at - state.started_at) * 1_000)
        )
        logger.info(
            "Cursor CLI invocation outcome=%s duration_ms=%d first_output_ms=%s "
            "stdout_bytes=%d prompt_bytes=%d prompt_limit_bytes=%d model=%s "
            "session_reused=%s",
            outcome,
            round((now - state.started_at) * 1_000),
            first_output_ms,
            state.stdout_bytes,
            state.prompt_bytes,
            state.prompt_limit_bytes,
            state.selected_model,
            state.resumed,
        )

    @staticmethod
    def _exit_error(returncode: int | None, stderr: bytes) -> CursorCliError:
        normalized = stderr.decode("utf-8", errors="replace").lower()
        stale = any(
            marker in normalized
            for marker in ("session not found", "chat not found", "does not exist")
        )
        return CursorCliError(
            f"Cursor CLI exited with code {returncode}. Stderr detail available: "
            f"{bool(stderr)}.",
            status_code=404 if stale else None,
        )

    async def _close_state(
        self, state: _CliProcessState, *, terminate: bool = False
    ) -> bytes:
        if state.closed:
            return b""
        state.closed = True
        if terminate:
            await terminate_process_tree(state.process)
        elif state.process.returncode is None:
            try:
                await asyncio.wait_for(
                    state.process.wait(), timeout=self._remaining(state)
                )
            except TimeoutError:
                await terminate_process_tree(state.process)
        if not state.stderr_task.done() and terminate:
            state.stderr_task.cancel()
        stderr_result = await asyncio.gather(state.stderr_task, return_exceptions=True)
        self._semaphore.release()
        value = stderr_result[0]
        return value if isinstance(value, bytes) else b""

    def _command(
        self, selection: CursorModelSelection, resume_id: str | None
    ) -> list[str]:
        if selection.model_id and (
            len(selection.model_id) > 200
            or any(ord(char) < 0x20 for char in selection.model_id)
        ):
            raise CursorCliError("Cursor CLI model must be a printable identifier.")
        if resume_id is not None and (
            not resume_id
            or len(resume_id) > 500
            or any(ord(char) < 0x20 for char in resume_id)
        ):
            raise CursorCliError("Invalid Cursor CLI session identifier.")
        command = [
            self.settings.cursor_cli,
            "--endpoint",
            CURSOR_CLI_ENDPOINT,
            "-p",
            "--trust",
            "--sandbox",
            "enabled",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
        ]
        if selection.model_id:
            command.extend(["--model", selection.model_id])
        if resume_id is not None:
            command.extend(["--resume", resume_id])
        return command

    async def _start(
        self,
        prompt: str,
        selection: CursorModelSelection,
        *,
        resume_id: str | None,
    ) -> CursorRun:
        encoded = prompt.encode("utf-8")
        prompt_limit = cursor_prompt_byte_limit(
            selection, self.settings.cursor_max_prompt_bytes
        )
        if len(encoded) > prompt_limit:
            raise CursorCliError(
                f"Rendered prompt is {len(encoded)} bytes; limit is "
                f"{prompt_limit} for the selected Cursor model.",
                status_code=400,
            )
        api_key, timeout_seconds = self._connection()
        workdir = self._workdir()
        await self._semaphore.acquire()
        process: asyncio.subprocess.Process | None = None
        state: _CliProcessState | None = None
        started_at = asyncio.get_running_loop().time()
        logger.info(
            "Cursor CLI invocation started prompt_bytes=%d prompt_limit_bytes=%d "
            "context_window_tokens=%s model=%s session_reused=%s",
            len(encoded),
            prompt_limit,
            selection.context_window_tokens,
            selection.model_id or "cursor-default",
            resume_id is not None,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command(selection, resume_id),
                cwd=workdir,
                env=self._environment(api_key),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_NDJSON_LINE_BYTES + 1,
                **isolated_subprocess_kwargs(),
            )
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise CursorCliError("Cursor CLI pipes were unavailable.")
            stderr_task = asyncio.create_task(
                self._read_stderr_tail(process.stderr), name="cursor-cli-stderr"
            )
            state = _CliProcessState(
                process=process,
                stdout=process.stdout,
                stderr_task=stderr_task,
                deadline=asyncio.get_running_loop().time() + timeout_seconds,
                started_at=started_at,
                first_output_deadline=(
                    started_at + self.settings.cursor_first_output_timeout_seconds
                ),
                prompt_bytes=len(encoded),
                prompt_limit_bytes=prompt_limit,
                selected_model=selection.model_id or "cursor-default",
                resumed=resume_id is not None,
            )
            try:
                process.stdin.write(encoded + b"\n")
                await asyncio.wait_for(
                    process.stdin.drain(), timeout=self._remaining(state)
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

            while True:
                event = await self._read_event(state)
                if event is None:
                    stderr = await self._close_state(state)
                    raise self._exit_error(process.returncode, stderr)
                if event.get("type") != "system" or event.get("subtype") != "init":
                    continue
                session_id = event.get("session_id")
                if (
                    not isinstance(session_id, str)
                    or not session_id
                    or len(session_id) > 500
                    or any(ord(char) < 0x20 for char in session_id)
                ):
                    raise CursorCliError("Cursor CLI returned an invalid session ID.")
                reported_model = event.get("model")
                if not isinstance(reported_model, str):
                    reported_model = None
                state.initialized_at = asyncio.get_running_loop().time()
                logger.info(
                    "Cursor CLI initialized startup_ms=%d model=%s session_reused=%s",
                    round((state.initialized_at - state.started_at) * 1_000),
                    reported_model or selection.display_name,
                    resume_id is not None,
                )
                return CursorRun(
                    session_id,
                    f"cli_{uuid.uuid4().hex}",
                    resume_id is not None,
                    reported_model,
                    state,
                )
        except FileNotFoundError as exc:
            self._semaphore.release()
            raise CursorCliError(
                f"Cursor Agent CLI was not found: {self.settings.cursor_cli}"
            ) from exc
        except _CursorFirstOutputTimeout as exc:
            if state is not None:
                self._log_outcome(state, "first_output_timeout")
                await self._close_state(state, terminate=True)
            else:
                if process is not None:
                    await terminate_process_tree(process)
                self._semaphore.release()
            raise CursorCliError(
                "Cursor CLI produced no assistant output within "
                f"{self.settings.cursor_first_output_timeout_seconds:g} seconds. "
                "The prompt may exceed the selected model's context window.",
                status_code=400,
            ) from exc
        except TimeoutError as exc:
            if state is not None:
                self._log_outcome(state, "timeout")
                await self._close_state(state, terminate=True)
            else:
                if process is not None:
                    await terminate_process_tree(process)
                self._semaphore.release()
            raise CursorCliError(
                f"Cursor CLI timed out after {timeout_seconds} seconds."
            ) from exc
        except (asyncio.CancelledError, GeneratorExit):
            if state is not None:
                self._log_outcome(state, "cancelled")
                await self._close_state(state, terminate=True)
            else:
                if process is not None:
                    await terminate_process_tree(process)
                self._semaphore.release()
            raise
        except CursorCliError:
            if state is not None and not state.closed:
                self._log_outcome(state, "error")
                await self._close_state(state, terminate=True)
            elif state is None:
                if process is not None:
                    await terminate_process_tree(process)
                self._semaphore.release()
            raise
        except OSError as exc:
            if state is not None:
                await self._close_state(state, terminate=True)
            else:
                if process is not None:
                    await terminate_process_tree(process)
                self._semaphore.release()
            raise CursorCliError(
                f"Cursor CLI could not start: {type(exc).__name__}."
            ) from exc

    async def get_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        api_key, timeout_seconds = self._connection()
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cached = self._models_cache
        if not force and cached and cached[0] == key_hash and now - cached[1] < 300:
            return [dict(item) for item in cached[2]]

        async with self._models_lock:
            cached = self._models_cache
            now = time.monotonic()
            if not force and cached and cached[0] == key_hash and now - cached[1] < 300:
                return [dict(item) for item in cached[2]]
            workdir = self._workdir()
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    self.settings.cursor_cli,
                    "--endpoint",
                    CURSOR_CLI_ENDPOINT,
                    "--list-models",
                    cwd=workdir,
                    env=self._environment(api_key),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **isolated_subprocess_kwargs(),
                )
                if process.stdout is None or process.stderr is None:
                    raise CursorCliError("Cursor CLI catalog pipes were unavailable.")
                stdout_task = asyncio.create_task(
                    self._read_limited(process.stdout, _MAX_CATALOG_BYTES)
                )
                stderr_task = asyncio.create_task(
                    self._read_stderr_tail(process.stderr)
                )
                stdout, stderr, _ = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, process.wait()),
                    timeout=min(timeout_seconds, 60),
                )
            except FileNotFoundError as exc:
                raise CursorCliError(
                    f"Cursor Agent CLI was not found: {self.settings.cursor_cli}"
                ) from exc
            except TimeoutError as exc:
                if process is not None:
                    await terminate_process_tree(process)
                raise CursorCliError("Cursor CLI model catalog timed out.") from exc
            except CursorCliError:
                if process is not None:
                    await terminate_process_tree(process)
                raise
            except OSError as exc:
                if process is not None:
                    await terminate_process_tree(process)
                raise CursorCliError(
                    f"Cursor CLI model catalog failed: {type(exc).__name__}."
                ) from exc
            if process.returncode != 0:
                raise self._exit_error(process.returncode, stderr)
            models = parse_cursor_cli_models(stdout.decode("utf-8", errors="replace"))
            self._models_cache = (key_hash, time.monotonic(), models)
            return [dict(item) for item in models]

    async def effective_selection(self, body: dict[str, Any]) -> CursorModelSelection:
        models = await self.get_models()
        return cli_selection_from_config(self.store.read()["cursor"], body, models)

    async def create_agent(
        self, prompt: str, selection: CursorModelSelection
    ) -> CursorRun:
        return await self._start(prompt, selection, resume_id=None)

    async def create_run(
        self,
        agent_id: str,
        prompt: str,
        selection: CursorModelSelection | None = None,
    ) -> CursorRun:
        if selection is None:
            selection = await self.effective_selection({})
        return await self._start(prompt, selection, resume_id=agent_id)

    @staticmethod
    def _state(run: CursorRun) -> _CliProcessState:
        if not isinstance(run.state, _CliProcessState):
            raise CursorCliError("Cursor CLI run state is unavailable.")
        return run.state

    @staticmethod
    def _assistant_text(event: dict[str, Any]) -> str | None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        return "".join(parts) if parts else None

    @staticmethod
    def _pending_delta(state: _CliProcessState) -> str:
        pending = state.pending_assistant
        state.pending_assistant = None
        if not pending:
            return ""
        if pending == state.emitted_text:
            return ""
        if pending.startswith(state.emitted_text):
            delta = pending[len(state.emitted_text) :]
        else:
            delta = pending
        state.emitted_text += delta
        return delta

    async def stream_run(self, run: CursorRun) -> AsyncIterator[CursorStreamEvent]:
        state = self._state(run)
        try:
            while True:
                event = await self._read_event(state)
                if event is None:
                    break
                event_type = event.get("type")
                if event_type == "assistant":
                    text = self._assistant_text(event)
                    if text is None:
                        continue
                    self._record_first_output(state)
                    if state.pending_assistant is not None:
                        delta = self._pending_delta(state)
                        if delta:
                            yield CursorStreamEvent("assistant", {"text": delta})
                    state.pending_assistant = text
                elif event_type == "result":
                    self._record_first_output(state)
                    delta = self._pending_delta(state)
                    if delta:
                        yield CursorStreamEvent("assistant", {"text": delta})
                    result = event.get("result")
                    if not isinstance(result, str):
                        result = ""
                    success = (
                        event.get("subtype") == "success"
                        and event.get("is_error") is not True
                    )
                    state.usage = _usage_from_event(event.get("usage"))
                    if state.usage is not None:
                        self._last_usage = dict(state.usage)
                    state.terminal_seen = True
                    yield CursorStreamEvent(
                        "result",
                        {
                            "status": "FINISHED" if success else "FAILED",
                            "text": result,
                        },
                    )

            stderr = await self._close_state(state)
            if state.process.returncode != 0:
                raise self._exit_error(state.process.returncode, stderr)
            if not state.terminal_seen:
                raise CursorCliError("Cursor CLI stream ended without a result event.")
            self._log_outcome(state, "completed")
        except _CursorFirstOutputTimeout as exc:
            self._log_outcome(state, "first_output_timeout")
            await self._close_state(state, terminate=True)
            raise CursorCliError(
                "Cursor CLI produced no assistant output within "
                f"{self.settings.cursor_first_output_timeout_seconds:g} seconds. "
                "The prompt may exceed the selected model's context window.",
                status_code=400,
            ) from exc
        except TimeoutError as exc:
            self._log_outcome(state, "timeout")
            await self._close_state(state, terminate=True)
            raise CursorCliError("Cursor CLI run timed out.") from exc
        except (asyncio.CancelledError, GeneratorExit):
            self._log_outcome(state, "cancelled")
            await self._close_state(state, terminate=True)
            raise
        except CursorCliError:
            self._log_outcome(state, "error")
            await self._close_state(state, terminate=True)
            raise
        except Exception as exc:
            self._log_outcome(state, "error")
            await self._close_state(state, terminate=True)
            raise CursorCliError(
                f"Cursor CLI stream failed: {type(exc).__name__}."
            ) from exc

    async def cancel_run(self, run: CursorRun) -> None:
        try:
            state = self._state(run)
        except CursorCliError:
            return
        self._log_outcome(state, "cancelled")
        await self._close_state(state, terminate=True)

    async def usage(self, run: CursorRun) -> dict[str, Any] | None:
        state = self._state(run)
        if state.usage is None:
            return None
        result = dict(state.usage)
        result.pop("cursor_cli_details", None)
        return result

    async def quota(self, *, force: bool = False) -> dict[str, Any]:
        await self.get_models(force=force)
        result: dict[str, Any] = {
            "status": "unsupported",
            "source": "Cursor Agent CLI",
            "note": (
                "Cursor CLI does not expose remaining account quota. Recent run "
                "token usage is shown after a successful request."
            ),
            "dashboard_url": "https://cursor.com/dashboard/usage",
            "account_verified": True,
        }
        if self._last_usage is not None:
            result["last_run_usage"] = dict(self._last_usage)
        return result
