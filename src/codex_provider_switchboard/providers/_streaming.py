from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..domain.bridge import encode_sse, output_item_events, response_object


async def with_response_heartbeat(
    iterator: AsyncIterator[bytes],
    heartbeat: Callable[[], bytes],
    *,
    interval_seconds: float,
) -> AsyncIterator[bytes]:
    """Emit real Responses events while an upstream event stream is silent."""

    upstream = aiter(iterator)
    pending: asyncio.Task[bytes] | None = None
    started = False
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(upstream))
            done, _ = await asyncio.wait({pending}, timeout=interval_seconds)
            if not done:
                if started:
                    yield heartbeat()
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            started = True
            yield chunk
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(upstream, "aclose", None)
        if close is not None:
            await close()


class ResponseEventStream:
    """Create a coherent OpenAI Responses SSE lifecycle."""

    def __init__(
        self,
        body: dict[str, Any],
        model: str,
        *,
        error_code: str,
    ) -> None:
        self.body = body
        self.model = model
        self.error_code = error_code
        self.response_id = f"resp_{os.urandom(16).hex()}"
        self.created = response_object(
            body, model, self.response_id, "in_progress", [], None
        )
        self._sequence = 0

    def event(self, event_type: str, **values: Any) -> bytes:
        event = {
            "type": event_type,
            "sequence_number": self._sequence,
            **values,
        }
        self._sequence += 1
        return next(iter(encode_sse([event])))

    def in_progress(self) -> bytes:
        return self.event("response.in_progress", response=self.created)

    def begin(self) -> tuple[bytes, bytes]:
        return (
            self.event("response.created", response=self.created),
            self.in_progress(),
        )

    def failed(self, message: str, *, code: str | None = None) -> bytes:
        failed = response_object(
            self.body,
            self.model,
            self.response_id,
            "failed",
            [],
            None,
        )
        failed["created_at"] = self.created["created_at"]
        failed["error"] = {"code": code or self.error_code, "message": message}
        return self.event("response.failed", response=failed)

    def start_message(
        self,
        item_id: str,
        *,
        phase: str,
        output_index: int = 0,
    ) -> tuple[bytes, bytes]:
        pending_item = {
            "id": item_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "phase": phase,
            "content": [],
        }
        return (
            self.event(
                "response.output_item.added",
                output_index=output_index,
                item=pending_item,
            ),
            self.event(
                "response.content_part.added",
                item_id=item_id,
                output_index=output_index,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ),
        )

    def start_reasoning(
        self,
        item_id: str,
        *,
        output_index: int = 0,
    ) -> tuple[bytes, bytes]:
        item = {
            "id": item_id,
            "type": "reasoning",
            "status": "in_progress",
            "summary": [],
        }
        return (
            self.event(
                "response.output_item.added",
                output_index=output_index,
                item=item,
            ),
            self.event(
                "response.reasoning_summary_part.added",
                item_id=item_id,
                output_index=output_index,
                summary_index=0,
                part={"type": "summary_text", "text": ""},
            ),
        )

    def reasoning_delta(
        self,
        item_id: str,
        delta: str,
        *,
        output_index: int = 0,
    ) -> bytes:
        return self.event(
            "response.reasoning_summary_text.delta",
            item_id=item_id,
            output_index=output_index,
            summary_index=0,
            delta=delta,
        )

    def finish_reasoning(
        self,
        item_id: str,
        text: str,
        *,
        output_index: int = 0,
        encrypted_content: str | None = None,
    ) -> tuple[list[bytes], dict[str, Any]]:
        part = {"type": "summary_text", "text": text}
        item: dict[str, Any] = {
            "id": item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [part],
        }
        if encrypted_content:
            item["encrypted_content"] = encrypted_content
        events = [
            self.event(
                "response.reasoning_summary_text.done",
                item_id=item_id,
                output_index=output_index,
                summary_index=0,
                text=text,
            ),
            self.event(
                "response.reasoning_summary_part.done",
                item_id=item_id,
                output_index=output_index,
                summary_index=0,
                part=part,
            ),
            self.event(
                "response.output_item.done",
                output_index=output_index,
                item=item,
            ),
        ]
        return events, item

    def text_delta(
        self,
        item_id: str,
        delta: str,
        *,
        output_index: int = 0,
    ) -> bytes:
        return self.event(
            "response.output_text.delta",
            item_id=item_id,
            output_index=output_index,
            content_index=0,
            delta=delta,
            logprobs=[],
        )

    def finish_message(
        self,
        item_id: str,
        text: str,
        *,
        phase: str,
        output_index: int = 0,
    ) -> tuple[list[bytes], dict[str, Any]]:
        content = {
            "type": "output_text",
            "text": text,
            "annotations": [],
            "logprobs": [],
        }
        item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "phase": phase,
            "content": [content],
        }
        events = [
            self.event(
                "response.output_text.done",
                item_id=item_id,
                output_index=output_index,
                content_index=0,
                text=text,
                logprobs=[],
            ),
            self.event(
                "response.content_part.done",
                item_id=item_id,
                output_index=output_index,
                content_index=0,
                part=content,
            ),
            self.event(
                "response.output_item.done",
                output_index=output_index,
                item=item,
            ),
        ]
        return events, item

    def completed_items(
        self,
        items: list[dict[str, Any]],
        *,
        start_index: int = 0,
    ) -> list[bytes]:
        events: list[bytes] = []
        for output_index, item in enumerate(items, start=start_index):
            for event_type, values in output_item_events(item, output_index):
                events.append(self.event(event_type, **values))
        return events

    def completed(
        self,
        items: list[dict[str, Any]],
        usage: dict[str, Any] | None,
    ) -> bytes:
        response = response_object(
            self.body,
            self.model,
            self.response_id,
            "completed",
            items,
            usage,
        )
        response["created_at"] = self.created["created_at"]
        return self.event("response.completed", response=response)
