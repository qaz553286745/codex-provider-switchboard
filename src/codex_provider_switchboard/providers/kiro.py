from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from ..application.inspector import RequestInspector
from ..domain.bridge import (
    BridgePromptTooLargeError,
    BridgeProtocolError,
    BridgeResult,
    BridgeUpstreamRetryableError,
    StreamingMessageParser,
    clean_kiro_stdout,
    collect_request_tools,
    kiro_effort_from_body,
    new_nonce,
    output_items,
    parse_bridge_output,
    render_bridge_prompt,
    response_object,
)
from ..infrastructure.config_store import ConfigStore
from ..infrastructure.kiro_cli import KiroInvocationError, KiroRunner
from ..infrastructure.session_cache import SessionCache, SessionLease
from ..settings import AppSettings
from ._streaming import ResponseEventStream, with_response_heartbeat
from .base import ProviderError, ProviderResponse

logger = logging.getLogger(__name__)
_RESUMED_SESSION_STALL_SECONDS = 120.0
_RESPONSE_HEARTBEAT_SECONDS = 30.0

_EXEC_DOT_CALL_RE = re.compile(r"\btools\.[A-Za-z_$][\w$]*\s*\(")
_EXEC_BRACKET_CALL_RE = re.compile(
    r"\btools\s*\[\s*[\"'][A-Za-z_$][\w$]*[\"']\s*\]\s*\("
)


def estimated_operation_count(result: BridgeResult) -> int:
    """Estimate logical operations represented by top-level tool calls."""
    total = 0
    for call in result.tool_calls:
        if call.name == "exec" and call.tool_type == "custom":
            nested = len(_EXEC_DOT_CALL_RE.findall(call.payload))
            nested += len(_EXEC_BRACKET_CALL_RE.findall(call.payload))
            total += max(1, nested)
        else:
            total += 1
    return total


class KiroProvider:
    provider_id = "kiro"

    def __init__(
        self,
        settings: AppSettings,
        store: ConfigStore,
        runner: KiroRunner,
        session_cache: SessionCache,
        inspector: RequestInspector,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runner = runner
        self.session_cache = session_cache
        self.inspector = inspector

    def model_id(self) -> str:
        return str(self.store.read()["kiro"]["model_id"])

    def _model_for(self, body: dict[str, Any]) -> str:
        requested = body.get("model")
        if self.settings.kiro_allow_requested_model and isinstance(requested, str):
            return requested
        return self.model_id()

    def _record(self, lease: SessionLease, model: str, effort: str | None) -> None:
        action = "resume_session" if lease.is_resume else "new_session"
        self.inspector.record(
            provider=self.provider_id,
            action=action,
            model=model,
            effort=effort,
            session_reused=lease.is_resume,
        )
        logger.info(
            "Kiro request started action=%s effort=%s", action, effort or "auto"
        )
        if lease.client_tool_gap_ms is not None:
            log = logger.warning if lease.client_tool_gap_ms >= 60_000 else logger.info
            log(
                "Kiro client tool result received client_tool_gap_ms=%d "
                "warning_threshold_ms=60000",
                lease.client_tool_gap_ms,
            )

    def _discard_contaminated_session(self, lease: SessionLease) -> None:
        try:
            lease.discard_mapping_for_retry()
        except OSError as exc:
            raise ProviderError(
                "Could not discard the contaminated Kiro session mapping.",
                error_type="kiro_session_error",
            ) from exc
        self.inspector.record(
            provider=self.provider_id,
            action="discard_contaminated_session",
            model=None,
            effort=None,
            session_reused=True,
        )
        logger.warning("Discarded a contaminated Kiro session mapping; retrying fresh")

    def _prepare_upstream_recovery(self, lease: SessionLease, *, reason: str) -> None:
        was_resumed = lease.is_resume
        try:
            lease.discard_mapping_for_retry()
        except OSError as exc:
            raise ProviderError(
                "Could not reset the overflowing Kiro session.",
                error_type="kiro_session_error",
            ) from exc
        self.inspector.record(
            provider=self.provider_id,
            action=(
                "context_recovery"
                if reason == "context_overflow"
                else "output_truncation_recovery"
            ),
            model=None,
            effort=None,
            session_reused=was_resumed,
        )
        logger.warning(
            "Kiro reported retryable upstream status reason=%s; retrying once "
            "with bounded history",
            reason,
        )

    @staticmethod
    def _upstream_retry_failed_message(reason: str) -> str:
        if reason == "output_truncated":
            return (
                "Kiro truncated its bridge output again after a bounded-history "
                "retry. Start a new Codex task or reduce the active "
                "instructions/tools."
            )
        return (
            "Kiro remained over its context window after a bounded-history "
            "retry. Start a new Codex task or reduce the active "
            "instructions/tools."
        )

    def _context_recovery_limit(self) -> int:
        return min(
            self.settings.kiro_max_prompt_bytes,
            self.settings.kiro_context_recovery_prompt_bytes,
        )

    def _resumed_session_stall_timeout(self, resumed_attempt: bool) -> float | None:
        if (
            not resumed_attempt
            or self.settings.kiro_timeout_seconds <= _RESUMED_SESSION_STALL_SECONDS
        ):
            return None
        return _RESUMED_SESSION_STALL_SECONDS

    def _prepare_stall_recovery(
        self, lease: SessionLease, timeout_seconds: float
    ) -> None:
        try:
            lease.discard_mapping_for_retry()
        except OSError as exc:
            raise ProviderError(
                "Could not reset the stalled Kiro session.",
                error_type="kiro_session_error",
            ) from exc
        self.inspector.record(
            provider=self.provider_id,
            action="stalled_session_recovery",
            model=None,
            effort=None,
            session_reused=True,
        )
        logger.warning(
            "Kiro resumed session exceeded stall timeout; retrying once fresh "
            "timeout_seconds=%g",
            timeout_seconds,
        )

    def _record_tool_metrics(
        self,
        lease: SessionLease,
        result: BridgeResult,
        round_duration_ms: int,
    ) -> None:
        operation_count = estimated_operation_count(result)
        stats = lease.record_tool_result(
            tool_call_count=len(result.tool_calls),
            estimated_operation_count=operation_count,
            round_duration_ms=round_duration_ms,
        )
        scheduling_mode = (
            "batched" if self.settings.kiro_tool_batching else "serial-baseline"
        )
        if stats.tool_round_index is not None:
            logger.info(
                "Kiro tool round completed scheduling_mode=%s "
                "tool_round_index=%d tool_call_count=%d "
                "estimated_operation_count=%d estimated_saved_rounds=%d "
                "cumulative_tool_calls=%d cumulative_estimated_operations=%d "
                "cumulative_saved_rounds=%d round_duration_ms=%d "
                "task_elapsed_ms=%d provider_duration_total_ms=%d",
                scheduling_mode,
                stats.tool_round_index,
                stats.tool_call_count,
                stats.estimated_operation_count,
                max(stats.estimated_operation_count - 1, 0),
                stats.total_tool_calls,
                stats.total_estimated_operations,
                stats.estimated_saved_rounds,
                stats.round_duration_ms,
                stats.task_elapsed_ms,
                stats.provider_duration_total_ms,
            )
        else:
            logger.info(
                "Kiro tool task completed scheduling_mode=%s "
                "tool_round_count=%d total_tool_calls=%d "
                "total_estimated_operations=%d estimated_saved_rounds=%d "
                "task_elapsed_ms=%d provider_duration_total_ms=%d",
                scheduling_mode,
                stats.tool_round_count,
                stats.total_tool_calls,
                stats.total_estimated_operations,
                stats.estimated_saved_rounds,
                stats.task_elapsed_ms,
                stats.provider_duration_total_ms,
            )

    def _record_tool_metrics(
        self,
        lease: SessionLease,
        result: BridgeResult,
        round_duration_ms: int,
    ) -> None:
        operation_count = estimated_operation_count(result)
        stats = lease.record_tool_result(
            tool_call_count=len(result.tool_calls),
            estimated_operation_count=operation_count,
            round_duration_ms=round_duration_ms,
        )
        scheduling_mode = (
            "batched" if self.settings.kiro_tool_batching else "serial-baseline"
        )
        if stats.tool_round_index is not None:
            logger.info(
                "Kiro tool round completed scheduling_mode=%s "
                "tool_round_index=%d tool_call_count=%d "
                "estimated_operation_count=%d estimated_saved_rounds=%d "
                "cumulative_tool_calls=%d cumulative_estimated_operations=%d "
                "cumulative_saved_rounds=%d round_duration_ms=%d "
                "task_elapsed_ms=%d provider_duration_total_ms=%d",
                scheduling_mode,
                stats.tool_round_index,
                stats.tool_call_count,
                stats.estimated_operation_count,
                max(stats.estimated_operation_count - 1, 0),
                stats.total_tool_calls,
                stats.total_estimated_operations,
                stats.estimated_saved_rounds,
                stats.round_duration_ms,
                stats.task_elapsed_ms,
                stats.provider_duration_total_ms,
            )
        else:
            logger.info(
                "Kiro tool task completed scheduling_mode=%s "
                "tool_round_count=%d total_tool_calls=%d "
                "total_estimated_operations=%d estimated_saved_rounds=%d "
                "task_elapsed_ms=%d provider_duration_total_ms=%d",
                scheduling_mode,
                stats.tool_round_count,
                stats.total_tool_calls,
                stats.total_estimated_operations,
                stats.estimated_saved_rounds,
                stats.task_elapsed_ms,
                stats.provider_duration_total_ms,
            )

    async def _commit(
        self,
        lease: SessionLease,
        completed_items: list[dict[str, Any]],
    ) -> None:
        if not lease.reusable:
            return
        session_id = lease.resume_id
        try:
            lease.commit(completed_items, session_id)
        except OSError as exc:
            logger.warning("Kiro session mapping commit failed: %s", type(exc).__name__)

    async def complete(self, body: dict[str, Any]) -> ProviderResponse:
        request_started = time.monotonic()
        model = self._model_for(body)
        effort = kiro_effort_from_body(body)
        lease = await self.session_cache.acquire(body)
        try:
            retry_resume = lease.is_resume
            upstream_recovery_attempted = False
            prompt_limit = self.settings.kiro_max_prompt_bytes
            while True:
                nonce = new_nonce()
                try:
                    prompt = render_bridge_prompt(
                        lease.request_body,
                        nonce,
                        tool_source_body=lease.body,
                        continuation=lease.continuation,
                        tool_batching=self.settings.kiro_tool_batching,
                        max_bytes=prompt_limit,
                    )
                except BridgePromptTooLargeError as exc:
                    raise ProviderError(
                        str(exc),
                        error_type="invalid_request_error",
                        status_code=400,
                    ) from exc
                self._record(lease, model, effort)
                try:
                    stall_timeout = self._resumed_session_stall_timeout(lease.is_resume)
                    async with asyncio.timeout(stall_timeout):
                        raw_output = await self.runner.generate(
                            prompt,
                            model,
                            effort,
                            workdir=lease.workdir,
                            resume_id=lease.resume_id,
                            resume_latest=lease.resume_latest,
                        )
                except TimeoutError:
                    if retry_resume and lease.is_resume and stall_timeout is not None:
                        self._prepare_stall_recovery(lease, stall_timeout)
                        retry_resume = False
                        continue
                    raise
                except KiroInvocationError as exc:
                    if exc.busy:
                        raise ProviderError(
                            str(exc),
                            error_type="provider_busy",
                            status_code=503,
                        ) from exc
                    if exc.terminal:
                        raise ProviderError(
                            str(exc),
                            error_type="invalid_request_error",
                            status_code=400,
                        ) from exc
                    if retry_resume and exc.stdout_bytes == 0:
                        self._discard_contaminated_session(lease)
                        retry_resume = False
                        continue
                    raise ProviderError(str(exc), error_type="kiro_cli_error") from exc
                try:
                    result = parse_bridge_output(
                        raw_output, collect_request_tools(body), nonce
                    )
                except BridgeUpstreamRetryableError as exc:
                    if upstream_recovery_attempted:
                        raise ProviderError(
                            self._upstream_retry_failed_message(exc.reason),
                            error_type="invalid_request_error",
                            status_code=400,
                        ) from exc
                    self._prepare_upstream_recovery(lease, reason=exc.reason)
                    upstream_recovery_attempted = True
                    prompt_limit = self._context_recovery_limit()
                    retry_resume = False
                    continue
                except BridgeProtocolError as exc:
                    if retry_resume and lease.is_resume:
                        self._discard_contaminated_session(lease)
                        retry_resume = False
                        continue
                    raise ProviderError(
                        "Kiro returned contaminated bridge protocol output.",
                        error_type="kiro_protocol_error",
                    ) from exc
                break

            completed_items = output_items(result)
            self._record_tool_metrics(
                lease,
                result,
                int((time.monotonic() - request_started) / 0.001),
            )
            await self._commit(lease, completed_items)
            completed = response_object(
                body,
                model,
                f"resp_{os.urandom(16).hex()}",
                "completed",
                completed_items,
                None,
            )
            return ProviderResponse(
                completed, {"X-Switchboard-Provider": self.provider_id}
            )
        finally:
            await lease.close()

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        stream_started = time.monotonic()
        model = self._model_for(body)
        events = ResponseEventStream(body, model, error_code="kiro_cli_error")
        async for event in with_response_heartbeat(
            self._stream(body, events, stream_started, model),
            events.in_progress,
            interval_seconds=_RESPONSE_HEARTBEAT_SECONDS,
        ):
            yield event

    async def _stream(
        self,
        body: dict[str, Any],
        events: ResponseEventStream,
        stream_started: float,
        model: str,
    ) -> AsyncIterator[bytes]:
        effort = kiro_effort_from_body(body)
        lease = await self.session_cache.acquire(body)
        session_wait_ms = int((time.monotonic() - stream_started) * 1_000)
        try:
            for event in events.begin():
                yield event

            item_id = f"msg_{os.urandom(16).hex()}"
            retry_resume = lease.is_resume
            upstream_recovery_attempted = False
            prompt_limit = self.settings.kiro_max_prompt_bytes
            while True:
                resumed_attempt = lease.is_resume
                nonce = new_nonce()
                try:
                    prompt = render_bridge_prompt(
                        lease.request_body,
                        nonce,
                        tool_source_body=lease.body,
                        continuation=lease.continuation,
                        tool_batching=self.settings.kiro_tool_batching,
                        max_bytes=prompt_limit,
                    )
                except BridgePromptTooLargeError as exc:
                    yield events.failed(str(exc), code="invalid_prompt")
                    return
                self._record(lease, model, effort)
                parser = StreamingMessageParser(nonce)
                raw_parts: list[str] = []
                message_started = False
                first_visible_at: float | None = None

                try:
                    stall_timeout = self._resumed_session_stall_timeout(resumed_attempt)
                    async with asyncio.timeout(stall_timeout):
                        async for chunk in self.runner.stream(
                            prompt,
                            model,
                            effort,
                            workdir=lease.workdir,
                            resume_id=lease.resume_id,
                            resume_latest=lease.resume_latest,
                        ):
                            raw_parts.append(chunk)
                            delta = parser.feed(chunk)
                            if delta and not message_started:
                                for event in events.start_message(
                                    item_id,
                                    phase=parser.phase or "final_answer",
                                ):
                                    yield event
                                message_started = True
                            if delta and first_visible_at is None:
                                first_visible_at = time.monotonic()
                            if delta:
                                yield events.text_delta(item_id, delta)
                except TimeoutError:
                    if (
                        retry_resume
                        and resumed_attempt
                        and not message_started
                        and stall_timeout is not None
                    ):
                        try:
                            self._prepare_stall_recovery(lease, stall_timeout)
                        except ProviderError as recovery_error:
                            yield events.failed(str(recovery_error))
                            return
                        retry_resume = False
                        continue
                    yield events.failed(
                        "Kiro resumed session stalled before completion.",
                        code="provider_timeout",
                    )
                    return
                except KiroInvocationError as exc:
                    if exc.busy:
                        yield events.failed(str(exc), code="provider_busy")
                        return
                    if exc.terminal:
                        yield events.failed(str(exc), code="invalid_prompt")
                        return
                    if retry_resume and exc.stdout_bytes == 0 and not raw_parts:
                        try:
                            self._discard_contaminated_session(lease)
                        except ProviderError as discard_error:
                            yield events.failed(str(discard_error))
                            return
                        retry_resume = False
                        continue
                    yield events.failed(str(exc))
                    return
                cleaned = clean_kiro_stdout("".join(raw_parts))
                try:
                    result = parse_bridge_output(
                        cleaned, collect_request_tools(body), nonce
                    )
                except BridgeUpstreamRetryableError as exc:
                    if upstream_recovery_attempted:
                        yield events.failed(
                            self._upstream_retry_failed_message(exc.reason),
                            code="invalid_prompt",
                        )
                        return
                    try:
                        self._prepare_upstream_recovery(lease, reason=exc.reason)
                    except ProviderError as recovery_error:
                        yield events.failed(str(recovery_error))
                        return
                    upstream_recovery_attempted = True
                    prompt_limit = self._context_recovery_limit()
                    retry_resume = False
                    continue
                except BridgeProtocolError:
                    if retry_resume and resumed_attempt and not message_started:
                        try:
                            self._discard_contaminated_session(lease)
                        except ProviderError as discard_error:
                            yield events.failed(str(discard_error))
                            return
                        retry_resume = False
                        continue
                    yield events.failed(
                        "Kiro returned contaminated bridge protocol output."
                    )
                    return
                break

            if parser.started:
                final_answer = (
                    parser.phase == "final_answer"
                    and result.text is not None
                    and not result.tool_calls
                    and result.commentary is None
                    and result.text == parser.text
                )
                commentary = (
                    parser.phase == "commentary"
                    and result.text is None
                    and bool(result.tool_calls)
                    and result.commentary == parser.text
                )
                if (
                    parser.error is not None
                    or parser.protocol_contaminated
                    or not parser.done
                    or not (final_answer or commentary)
                ):
                    detail = parser.error or (
                        "Kiro returned an invalid streamed visible-message envelope."
                    )
                    yield events.failed(detail)
                    return
                if not message_started:
                    for event in events.start_message(
                        item_id,
                        phase=parser.phase or "final_answer",
                    ):
                        yield event
                    message_started = True
                final_events, completed_item = events.finish_message(
                    item_id,
                    parser.text,
                    phase=parser.phase or "final_answer",
                )
                for event in final_events:
                    yield event
                if commentary:
                    parsed_items = output_items(result)
                    completed_items = [completed_item, *parsed_items[1:]]
                    for event in events.completed_items(
                        completed_items[1:],
                        start_index=1,
                    ):
                        yield event
                else:
                    completed_items = [completed_item]
            else:
                completed_items = output_items(result)
                for event in events.completed_items(completed_items):
                    yield event

            bookkeeping_started_at = time.monotonic()
            self._record_tool_metrics(
                lease,
                result,
                int((bookkeeping_started_at - stream_started) / 0.001),
            )
            await self._commit(lease, completed_items)
            response_completed_at = time.monotonic()
            logger.info(
                "Kiro stream completed duration_ms=%d session_wait_ms=%d "
                "first_visible_ms=%s response_completed_ms=%d bookkeeping_ms=%d "
                "output_kind=%s session_reused=%s",
                int((time.monotonic() - stream_started) * 1_000),
                session_wait_ms,
                (
                    str(int((first_visible_at - stream_started) * 1_000))
                    if first_visible_at is not None
                    else "none"
                ),
                int((response_completed_at - stream_started) / 0.001),
                int((response_completed_at - bookkeeping_started_at) / 0.001),
                "message" if result.text is not None else "tool_calls",
                lease.is_resume,
            )
            yield events.completed(
                completed_items,
                None,
            )
        finally:
            await lease.close()
