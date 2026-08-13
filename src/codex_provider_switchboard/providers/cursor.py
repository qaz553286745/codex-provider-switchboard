from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from ..application.inspector import RequestInspector
from ..domain.bridge import (
    BridgePromptTooLargeError,
    BridgeProtocolError,
    StreamingMessageParser,
    codex_thread_key,
    collect_request_tools,
    new_nonce,
    output_items,
    parse_bridge_output,
    render_bridge_prompt,
    response_object,
)
from ..infrastructure.config_store import ConfigStore
from ..infrastructure.cursor_cli import (
    CURSOR_CLI_SESSION_PROFILE,
    CursorCliRunner,
    cursor_prompt_byte_limit,
)
from ..infrastructure.cursor_client import (
    CursorBackendError,
    CursorClient,
    CursorModelSelection,
    CursorRun,
)
from ..infrastructure.session_cache import SessionCache, SessionLease
from ._streaming import ResponseEventStream
from .base import ProviderError, ProviderResponse

logger = logging.getLogger(__name__)


class CursorProvider:
    provider_id = "cursor"

    def __init__(
        self,
        store: ConfigStore,
        client: CursorClient,
        cli_runner: CursorCliRunner,
        session_cache: SessionCache,
        inspector: RequestInspector,
    ) -> None:
        self.store = store
        self.client = client
        self.cli_runner = cli_runner
        self.session_cache = session_cache
        self.inspector = inspector

    def backend_id(self) -> str:
        value = self.store.read()["cursor"].get("backend")
        return "cloud_api" if value == "cloud_api" else "cli"

    def _backend(self) -> CursorClient | CursorCliRunner:
        return self.client if self.backend_id() == "cloud_api" else self.cli_runner

    def model_id(self) -> str:
        return str(self.store.read()["cursor"].get("model_id") or "cursor-default")

    @staticmethod
    def _response_model(selection: CursorModelSelection) -> str:
        return selection.model_id or "cursor-default"

    @staticmethod
    def _cache_body(
        body: dict[str, Any],
        selection: CursorModelSelection,
        backend_id: str,
    ) -> dict[str, Any]:
        thread_key = codex_thread_key(body)
        if thread_key is None:
            return body
        client_metadata = body.get("client_metadata")
        metadata = dict(client_metadata) if isinstance(client_metadata, dict) else {}
        session_profile = (
            f"{backend_id}:{CURSOR_CLI_SESSION_PROFILE}"
            if backend_id == "cli"
            else backend_id
        )
        metadata["thread_id"] = (
            f"cursor-provider:{session_profile}:{selection.fingerprint}:{thread_key}"
        )
        return {**body, "client_metadata": metadata}

    async def _start_run(
        self,
        backend: CursorClient | CursorCliRunner,
        lease: SessionLease,
        selection: CursorModelSelection,
    ) -> tuple[CursorRun, str, str]:
        if lease.resume_latest:
            lease.reset_for_retry()
        retry_stale = lease.resume_id is not None
        while True:
            nonce = new_nonce()
            prompt = render_bridge_prompt(
                lease.request_body,
                nonce,
                tool_source_body=lease.body,
                continuation=lease.continuation,
                runtime_name=backend.runtime_name,
                session_name=backend.session_name,
                max_bytes=(
                    cursor_prompt_byte_limit(
                        selection, self.cli_runner.settings.cursor_max_prompt_bytes
                    )
                    if backend.backend_id == "cli"
                    else None
                ),
            )
            try:
                if lease.resume_id is not None:
                    run = await backend.create_run(lease.resume_id, prompt, selection)
                else:
                    run = await backend.create_agent(prompt, selection)
            except CursorBackendError as exc:
                if retry_stale and exc.status_code in {404, 410}:
                    lease.reset_for_retry()
                    retry_stale = False
                    continue
                raise

            self.inspector.record(
                provider=self.provider_id,
                action=(
                    f"{backend.backend_id}_resume_session"
                    if run.is_continuation
                    else f"{backend.backend_id}_new_session"
                ),
                model={
                    "id": selection.model_id or None,
                    "params": [
                        {"id": param_id, "value": value}
                        for param_id, value in selection.params
                    ],
                    "display_name": run.reported_model or selection.display_name,
                },
                session_reused=run.is_continuation,
            )
            return run, nonce, prompt

    @staticmethod
    def _terminal_output(
        final_status: str | None,
        final_text: str | None,
        raw_parts: list[str],
    ) -> str:
        if final_status != "FINISHED":
            raise CursorBackendError(
                f"Cursor run did not finish: "
                f"{final_status or 'missing terminal result'}."
            )
        raw_output = final_text if final_text is not None else "".join(raw_parts)
        if not raw_output:
            raise CursorBackendError("Cursor returned an empty result.")
        return raw_output

    def _commit(
        self,
        lease: SessionLease,
        completed_items: list[dict[str, Any]],
        agent_id: str,
    ) -> None:
        try:
            lease.commit(completed_items, agent_id)
        except OSError as exc:
            logger.warning(
                "Cursor session mapping commit failed: %s", type(exc).__name__
            )

    async def complete(self, body: dict[str, Any]) -> ProviderResponse:
        backend = self._backend()
        try:
            selection = await backend.effective_selection(body)
        except CursorBackendError as exc:
            raise ProviderError(
                str(exc),
                error_type=(
                    "invalid_request_error"
                    if exc.status_code is not None and 400 <= exc.status_code < 500
                    else f"cursor_{backend.backend_id}_error"
                ),
                status_code=(
                    exc.status_code
                    if exc.status_code is not None and 400 <= exc.status_code < 500
                    else 502
                ),
            ) from exc
        lease = await self.session_cache.acquire(
            self._cache_body(body, selection, backend.backend_id)
        )
        run: CursorRun | None = None
        try:
            try:
                run, nonce, _prompt = await self._start_run(backend, lease, selection)
                raw_parts: list[str] = []
                final_text: str | None = None
                final_status: str | None = None
                async for upstream in backend.stream_run(run):
                    if upstream.event == "assistant":
                        value = upstream.data.get("text")
                        if isinstance(value, str):
                            raw_parts.append(value)
                    elif upstream.event == "result":
                        value = upstream.data.get("text")
                        if isinstance(value, str):
                            final_text = value
                        status = upstream.data.get("status")
                        if isinstance(status, str):
                            final_status = status.upper()
                raw_output = self._terminal_output(final_status, final_text, raw_parts)
            except asyncio.CancelledError:
                if run is not None:
                    await backend.cancel_run(run)
                raise
            except BridgePromptTooLargeError as exc:
                raise ProviderError(
                    str(exc),
                    error_type="invalid_request_error",
                    status_code=400,
                ) from exc
            except CursorBackendError as exc:
                if run is not None:
                    await backend.cancel_run(run)
                raise ProviderError(
                    str(exc),
                    error_type=(
                        "invalid_request_error"
                        if exc.status_code is not None and 400 <= exc.status_code < 500
                        else f"cursor_{backend.backend_id}_error"
                    ),
                    status_code=(
                        exc.status_code
                        if exc.status_code is not None and 400 <= exc.status_code < 500
                        else 502
                    ),
                ) from exc

            try:
                result = parse_bridge_output(
                    raw_output, collect_request_tools(body), nonce
                )
            except BridgeProtocolError as exc:
                try:
                    lease.discard_mapping_for_retry()
                except OSError:
                    logger.warning("Cursor session mapping cleanup failed")
                raise ProviderError(
                    "Cursor returned contaminated bridge protocol output.",
                    error_type=f"cursor_{backend.backend_id}_protocol_error",
                ) from exc
            completed_items = output_items(result)
            self._commit(lease, completed_items, run.agent_id)
            usage = await backend.usage(run)
            completed = response_object(
                body,
                self._response_model(selection),
                f"resp_{os.urandom(16).hex()}",
                "completed",
                completed_items,
                usage,
            )
            return ProviderResponse(
                completed, {"X-Switchboard-Provider": self.provider_id}
            )
        finally:
            await lease.close()

    async def stream(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        backend = self._backend()
        error_code = f"cursor_{backend.backend_id}_error"
        try:
            selection = await backend.effective_selection(body)
        except CursorBackendError as exc:
            events = ResponseEventStream(body, self.model_id(), error_code=error_code)
            for event in events.begin():
                yield event
            yield events.failed(
                str(exc),
                code=(
                    "invalid_prompt"
                    if exc.status_code is not None and 400 <= exc.status_code < 500
                    else None
                ),
            )
            return
        model = self._response_model(selection)
        events = ResponseEventStream(body, model, error_code=error_code)
        lease = await self.session_cache.acquire(
            self._cache_body(body, selection, backend.backend_id)
        )
        run: CursorRun | None = None
        try:
            for event in events.begin():
                yield event
            try:
                run, nonce, _prompt = await self._start_run(backend, lease, selection)
            except BridgePromptTooLargeError as exc:
                yield events.failed(str(exc), code="invalid_prompt")
                return
            except CursorBackendError as exc:
                yield events.failed(
                    str(exc),
                    code=(
                        "invalid_prompt"
                        if exc.status_code is not None and 400 <= exc.status_code < 500
                        else None
                    ),
                )
                return

            parser = StreamingMessageParser(nonce)
            raw_parts: list[str] = []
            final_text: str | None = None
            final_status: str | None = None
            message_started = False
            item_id = f"msg_{os.urandom(16).hex()}"

            try:
                async for upstream in backend.stream_run(run):
                    if upstream.event == "assistant":
                        text = upstream.data.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        raw_parts.append(text)
                        delta = parser.feed(text)
                        if delta and not message_started:
                            for event in events.start_message(
                                item_id,
                                phase=parser.phase or "final_answer",
                            ):
                                yield event
                            message_started = True
                        if delta:
                            yield events.text_delta(item_id, delta)
                    elif upstream.event == "result":
                        value = upstream.data.get("text")
                        if isinstance(value, str):
                            final_text = value
                        status = upstream.data.get("status")
                        if isinstance(status, str):
                            final_status = status.upper()
            except asyncio.CancelledError:
                await backend.cancel_run(run)
                raise
            except CursorBackendError as exc:
                await backend.cancel_run(run)
                yield events.failed(
                    str(exc),
                    code=(
                        "invalid_prompt"
                        if exc.status_code is not None and 400 <= exc.status_code < 500
                        else None
                    ),
                )
                return

            try:
                raw_output = self._terminal_output(final_status, final_text, raw_parts)
            except CursorBackendError as exc:
                yield events.failed(
                    str(exc),
                    code=(
                        "invalid_prompt"
                        if exc.status_code is not None and 400 <= exc.status_code < 500
                        else None
                    ),
                )
                return
            try:
                result = parse_bridge_output(
                    raw_output, collect_request_tools(body), nonce
                )
            except BridgeProtocolError:
                try:
                    lease.discard_mapping_for_retry()
                except OSError:
                    logger.warning("Cursor session mapping cleanup failed")
                yield events.failed(
                    "Cursor returned contaminated bridge protocol output."
                )
                return

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
                        "Cursor returned an invalid streamed visible-message envelope."
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

            self._commit(lease, completed_items, run.agent_id)
            usage = await backend.usage(run)
            yield events.completed(completed_items, usage)
        finally:
            await lease.close()

    async def get_models(self, *, force: bool = False) -> list[dict[str, Any]]:
        return await self._backend().get_models(force=force)

    async def quota(self, *, force: bool = False) -> dict[str, Any]:
        return await self._backend().quota(force=force)
