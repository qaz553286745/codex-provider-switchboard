from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.bridge import codex_thread_key_hash

_STATE_VERSION = 1
_MAX_INPUT_ITEMS = 10_000
_MAX_STATE_BYTES = 2 * 1_048_576
_MAX_PENDING_TOOL_CALLS = 32
_CALL_ID_HASH_LENGTH = 16
_HEX_DIGITS = frozenset("0123456789abcdef")
_TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

logger = logging.getLogger(__name__)


def _normalized_item(value: Any, depth: int = 0) -> Any:
    if isinstance(value, list):
        return [_normalized_item(item, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if depth == 0 and key in {"id", "status"}:
            continue
        if key in {"annotations", "logprobs"}:
            continue
        normalized[key] = _normalized_item(item, depth + 1)
    return normalized


def item_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _normalized_item(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def item_fingerprints(values: list[Any]) -> list[str]:
    return [item_fingerprint(value) for value in values]


@dataclass(frozen=True)
class PendingToolCall:
    item_type: str
    tool_name: str
    call_id_hash: str


def tool_call_id_hash(call_id: str) -> str:
    """Return a stable, non-reversible identifier suitable for diagnostics."""
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:_CALL_ID_HASH_LENGTH]


def _pending_tool_calls(
    output_items: list[dict[str, Any]],
) -> tuple[PendingToolCall, ...]:
    pending: list[PendingToolCall] = []
    for item in output_items:
        if len(pending) >= _MAX_PENDING_TOOL_CALLS:
            break
        item_type = item.get("type")
        tool_name = item.get("name")
        call_id = item.get("call_id")
        if (
            item_type not in _TOOL_CALL_TYPES
            or not isinstance(tool_name, str)
            or not isinstance(call_id, str)
        ):
            continue
        pending.append(
            PendingToolCall(
                item_type=item_type,
                tool_name=tool_name,
                call_id_hash=tool_call_id_hash(call_id),
            )
        )
    return tuple(pending)


def _tool_results_received(
    new_items: list[Any], pending: tuple[PendingToolCall, ...]
) -> tuple[PendingToolCall, ...] | None:
    result_hashes = {
        tool_call_id_hash(call_id)
        for item in new_items
        if isinstance(item, dict)
        and item.get("type") in _TOOL_OUTPUT_TYPES
        and isinstance((call_id := item.get("call_id")), str)
    }
    if not result_hashes:
        return None
    if not pending:
        return ()
    matched = tuple(item for item in pending if item.call_id_hash in result_hashes)
    return matched or None


def _metadata_for_log(pending: tuple[PendingToolCall, ...]) -> str:
    return json.dumps(
        [
            {
                "item_type": item.item_type,
                "tool_name": item.tool_name,
                "call_id_hash": item.call_id_hash,
            }
            for item in pending
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class _StoredState:
    session_id: str | None
    resume_latest: bool
    known_item_hashes: tuple[str, ...]
    updated_at: float
    task_started_at: float | None
    tool_round_count: int
    total_tool_calls: int
    total_estimated_operations: int
    estimated_saved_rounds: int
    provider_duration_ms: int
    awaiting_tool_since: float | None
    pending_tool_calls: tuple[PendingToolCall, ...]


@dataclass(frozen=True)
class ToolRoundStats:
    tool_round_index: int | None
    tool_round_count: int
    tool_call_count: int
    total_tool_calls: int
    estimated_operation_count: int
    total_estimated_operations: int
    estimated_saved_rounds: int
    round_duration_ms: int
    provider_duration_total_ms: int
    task_elapsed_ms: int


class SessionLease:
    def __init__(
        self,
        *,
        body: dict[str, Any],
        workdir: Path,
        state_path: Path | None,
        lock: asyncio.Lock | None,
        current_input_hashes: list[str],
        request_body: dict[str, Any],
        continuation: bool,
        resume_id: str | None,
        resume_latest: bool,
        task_started_at: float,
        tool_round_count: int,
        total_tool_calls: int,
        total_estimated_operations: int,
        estimated_saved_rounds: int,
        provider_duration_ms: int,
        client_tool_gap_ms: int | None,
        client_tool_calls: tuple[PendingToolCall, ...],
    ) -> None:
        self.body = body
        self.workdir = workdir
        self.state_path = state_path
        self._lock = lock
        self.current_input_hashes = current_input_hashes
        self.request_body = request_body
        self.continuation = continuation
        self.resume_id = resume_id
        self.resume_latest = resume_latest
        self.task_started_at = task_started_at
        self.tool_round_count = tool_round_count
        self.total_tool_calls = total_tool_calls
        self.total_estimated_operations = total_estimated_operations
        self.estimated_saved_rounds = estimated_saved_rounds
        self.provider_duration_ms = provider_duration_ms
        self.client_tool_gap_ms = client_tool_gap_ms
        self.client_tool_calls = client_tool_calls
        self._metrics_recorded = False
        self._closed = False

    @property
    def reusable(self) -> bool:
        return self.state_path is not None

    @property
    def is_resume(self) -> bool:
        return self.resume_id is not None or self.resume_latest

    def reset_for_retry(self) -> None:
        self.request_body = self.body
        self.continuation = False
        self.resume_id = None
        self.resume_latest = False

    def discard_mapping_for_retry(self) -> None:
        """Forget a contaminated upstream session and replay the full request."""
        if self.state_path is not None:
            self.state_path.unlink(missing_ok=True)
        self.reset_for_retry()

    def record_tool_result(
        self,
        *,
        tool_call_count: int,
        estimated_operation_count: int,
        round_duration_ms: int,
        completed_at: float | None = None,
    ) -> ToolRoundStats:
        """Update content-free task metrics once for one provider response."""
        if self._metrics_recorded:
            raise RuntimeError("Tool metrics were already recorded for this lease.")
        if (
            tool_call_count < 0
            or estimated_operation_count < tool_call_count
            or round_duration_ms < 0
        ):
            raise ValueError("Tool metrics are inconsistent.")
        self._metrics_recorded = True
        tool_round_index: int | None = None
        if tool_call_count:
            self.tool_round_count += 1
            tool_round_index = self.tool_round_count
            self.total_tool_calls += tool_call_count
            self.total_estimated_operations += estimated_operation_count
            self.estimated_saved_rounds += max(estimated_operation_count - 1, 0)
        self.provider_duration_ms += round_duration_ms
        finished_at = time.time() if completed_at is None else completed_at
        return ToolRoundStats(
            tool_round_index=tool_round_index,
            tool_round_count=self.tool_round_count,
            tool_call_count=tool_call_count,
            total_tool_calls=self.total_tool_calls,
            estimated_operation_count=estimated_operation_count,
            total_estimated_operations=self.total_estimated_operations,
            estimated_saved_rounds=self.estimated_saved_rounds,
            round_duration_ms=round_duration_ms,
            provider_duration_total_ms=self.provider_duration_ms,
            task_elapsed_ms=max(0, int((finished_at - self.task_started_at) * 1_000)),
        )

    def commit(
        self,
        output_items: list[dict[str, Any]],
        session_id: str | None,
    ) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_path.parent, 0o700)
        committed_at = time.time()
        pending_tool_calls = _pending_tool_calls(output_items)
        awaiting_tool_since = committed_at if pending_tool_calls else None
        if pending_tool_calls:
            logger.info(
                "Tool wait started pending_tool_count=%d pending_tools=%s",
                len(pending_tool_calls),
                _metadata_for_log(pending_tool_calls),
            )
        state = {
            "version": _STATE_VERSION,
            "session_id": session_id,
            "resume_latest": session_id is None,
            "known_item_hashes": self.current_input_hashes
            + item_fingerprints(output_items),
            "task_started_at": self.task_started_at,
            "tool_round_count": self.tool_round_count,
            "total_tool_calls": self.total_tool_calls,
            "total_estimated_operations": self.total_estimated_operations,
            "estimated_saved_rounds": self.estimated_saved_rounds,
            "provider_duration_ms": self.provider_duration_ms,
            "awaiting_tool_since": awaiting_tool_since,
            "pending_tool_calls": [
                {
                    "item_type": item.item_type,
                    "tool_name": item.tool_name,
                    "call_id_hash": item.call_id_hash,
                }
                for item in pending_tool_calls
            ],
            "updated_at": committed_at,
        }
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)
        os.chmod(self.state_path, 0o600)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lock is not None and self._lock.locked():
            self._lock.release()


class SessionCache:
    def __init__(
        self,
        base_workdir: Path,
        *,
        enabled: bool,
        ttl_seconds: float,
    ) -> None:
        self.base_workdir = base_workdir
        self.enabled = enabled
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def _load_state(self, path: Path) -> _StoredState | None:
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("version") != _STATE_VERSION:
            return None

        session_id = value.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str)
            or len(session_id) > 500
            or any(ord(char) < 0x20 for char in session_id)
        ):
            return None
        resume_latest = value.get("resume_latest") is True
        hashes = value.get("known_item_hashes")
        updated_at = value.get("updated_at")
        task_started_at = value.get("task_started_at")
        awaiting_tool_since = value.get("awaiting_tool_since")
        pending_value = value.get("pending_tool_calls", [])
        metric_names = (
            "tool_round_count",
            "total_tool_calls",
            "total_estimated_operations",
            "estimated_saved_rounds",
            "provider_duration_ms",
        )
        metrics = [value.get(name, 0) for name in metric_names]
        if (
            not isinstance(hashes, list)
            or len(hashes) > _MAX_INPUT_ITEMS + 64
            or not all(
                isinstance(item, str) and len(item) == 64 and set(item) <= _HEX_DIGITS
                for item in hashes
            )
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
            or (
                task_started_at is not None
                and (
                    not isinstance(task_started_at, (int, float))
                    or not math.isfinite(float(task_started_at))
                    or float(task_started_at) <= 0
                )
            )
            or not all(
                isinstance(metric, int)
                and not isinstance(metric, bool)
                and 0 <= metric <= 10**15
                for metric in metrics
            )
            or (session_id is None and not resume_latest)
            or not isinstance(pending_value, list)
            or len(pending_value) > _MAX_PENDING_TOOL_CALLS
            or not all(
                isinstance(item, dict)
                and set(item) == {"item_type", "tool_name", "call_id_hash"}
                and item.get("item_type") in _TOOL_CALL_TYPES
                and isinstance(item.get("tool_name"), str)
                and 0 < len(item["tool_name"]) <= 256
                and not any(ord(char) < 0x20 for char in item["tool_name"])
                and isinstance(item.get("call_id_hash"), str)
                and len(item["call_id_hash"]) == _CALL_ID_HASH_LENGTH
                and set(item["call_id_hash"]) <= _HEX_DIGITS
                for item in pending_value
            )
            or (
                awaiting_tool_since is not None
                and (
                    not isinstance(awaiting_tool_since, (int, float))
                    or not math.isfinite(float(awaiting_tool_since))
                    or float(awaiting_tool_since) <= 0
                )
            )
        ):
            return None
        if self.ttl_seconds and time.time() - float(updated_at) > self.ttl_seconds:
            return None
        return _StoredState(
            session_id=session_id,
            resume_latest=resume_latest,
            known_item_hashes=tuple(hashes),
            updated_at=float(updated_at),
            task_started_at=(
                float(task_started_at) if task_started_at is not None else None
            ),
            tool_round_count=metrics[0],
            total_tool_calls=metrics[1],
            total_estimated_operations=metrics[2],
            estimated_saved_rounds=metrics[3],
            provider_duration_ms=metrics[4],
            awaiting_tool_since=(
                float(awaiting_tool_since) if awaiting_tool_since is not None else None
            ),
            pending_tool_calls=tuple(
                PendingToolCall(
                    item_type=item["item_type"],
                    tool_name=item["tool_name"],
                    call_id_hash=item["call_id_hash"],
                )
                for item in pending_value
            ),
        )

    async def acquire(self, body: dict[str, Any]) -> SessionLease:
        acquired_at = time.time()
        key_hash = codex_thread_key_hash(body)
        input_value = body.get("input")
        if (
            not self.enabled
            or key_hash is None
            or not isinstance(input_value, list)
            or len(input_value) > _MAX_INPUT_ITEMS
        ):
            return SessionLease(
                body=body,
                workdir=self.base_workdir,
                state_path=None,
                lock=None,
                current_input_hashes=[],
                request_body=body,
                continuation=False,
                resume_id=None,
                resume_latest=False,
                task_started_at=acquired_at,
                tool_round_count=0,
                total_tool_calls=0,
                total_estimated_operations=0,
                estimated_saved_rounds=0,
                provider_duration_ms=0,
                client_tool_gap_ms=None,
                client_tool_calls=(),
            )

        lock = self._locks.setdefault(key_hash, asyncio.Lock())
        await lock.acquire()
        try:
            sessions_root = self.base_workdir / "codex-sessions"
            sessions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(sessions_root, 0o700)
            workdir = sessions_root / key_hash
            workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(workdir, 0o700)
            state_path = workdir / ".bridge-session.json"
            current_hashes = item_fingerprints(input_value)
            state = self._load_state(state_path)

            continuation = False
            request_body = body
            resume_id: str | None = None
            resume_latest = False
            task_started_at = acquired_at
            tool_round_count = 0
            total_tool_calls = 0
            total_estimated_operations = 0
            estimated_saved_rounds = 0
            provider_duration_ms = 0
            client_tool_gap_ms: int | None = None
            client_tool_calls: tuple[PendingToolCall, ...] = ()
            if state is not None:
                known = list(state.known_item_hashes)
                if (
                    known
                    and len(current_hashes) > len(known)
                    and current_hashes[: len(known)] == known
                ):
                    continuation = True
                    request_body = {**body, "input": input_value[len(known) :]}
                    resume_id = state.session_id
                    resume_latest = state.resume_latest
                    task_started_at = state.task_started_at or acquired_at
                    tool_round_count = state.tool_round_count
                    total_tool_calls = state.total_tool_calls
                    total_estimated_operations = state.total_estimated_operations
                    estimated_saved_rounds = state.estimated_saved_rounds
                    provider_duration_ms = state.provider_duration_ms
                    received = _tool_results_received(
                        input_value[len(known) :], state.pending_tool_calls
                    )
                    if state.awaiting_tool_since is not None and received is not None:
                        client_tool_gap_ms = max(
                            0,
                            int((acquired_at - state.awaiting_tool_since) * 1_000),
                        )
                        client_tool_calls = received
                        log = (
                            logger.warning
                            if client_tool_gap_ms >= 60_000
                            else logger.info
                        )
                        log(
                            "Client tool results received client_tool_gap_ms=%d "
                            "completed_tool_count=%d pending_tools=%s "
                            "warning_threshold_ms=60000",
                            client_tool_gap_ms,
                            len(client_tool_calls),
                            _metadata_for_log(client_tool_calls),
                        )

            return SessionLease(
                body=body,
                workdir=workdir,
                state_path=state_path,
                lock=lock,
                current_input_hashes=current_hashes,
                request_body=request_body,
                continuation=continuation,
                resume_id=resume_id,
                resume_latest=resume_latest,
                task_started_at=task_started_at,
                tool_round_count=tool_round_count,
                total_tool_calls=total_tool_calls,
                total_estimated_operations=total_estimated_operations,
                estimated_saved_rounds=estimated_saved_rounds,
                provider_duration_ms=provider_duration_ms,
                client_tool_gap_ms=client_tool_gap_ms,
                client_tool_calls=client_tool_calls,
            )
        except Exception:
            lock.release()
            raise
