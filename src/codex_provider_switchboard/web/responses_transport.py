from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect

from ..compatibility.responses import bind_transport_context
from ..domain.bridge import codex_thread_key_hash, encode_sse, response_object
from ..providers.base import ProviderError
from ..settings import AppSettings

logger = logging.getLogger(__name__)

_SSE_SEPARATOR = re.compile(rb"\r?\n\r?\n")
_STREAM_ID = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
_TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.incomplete", "error"}
)
_MAX_ACTIVE_RESPONSES = 16
_MAX_NAMED_STREAMS = 32
_MAX_QUEUED_RESPONSES = 512
_MAX_CACHED_RESPONSES = 128


class PayloadError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ClientDisconnected(Exception):
    """Internal signal that the WebSocket can no longer accept events."""


class _StreamingService(Protocol):
    def active_provider_id(self) -> str: ...

    def stream_for(
        self, provider_id: str, body: dict[str, Any]
    ) -> AsyncIterator[bytes]: ...


class SSEJSONDecoder:
    """Decode complete JSON SSE records without exposing partial data."""

    def __init__(self, limit: int) -> None:
        self._buffer = bytearray()
        self._limit = limit

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(chunk)
        events: list[dict[str, Any]] = []
        while match := _SSE_SEPARATOR.search(self._buffer):
            block = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            if len(block) > self._limit:
                raise PayloadError(
                    "Upstream stream event is too large.", status_code=502
                )
            event = self._decode_block(block)
            if event is not None:
                events.append(event)
        if len(self._buffer) > self._limit:
            raise PayloadError("Upstream stream event is too large.", status_code=502)
        return events

    def finish(self) -> None:
        if self._buffer.strip():
            raise PayloadError(
                "Upstream stream ended with an incomplete event.", status_code=502
            )

    @staticmethod
    def _decode_block(block: bytes) -> dict[str, Any] | None:
        try:
            lines = block.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise PayloadError(
                "Upstream stream was not valid UTF-8.", status_code=502
            ) from exc
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        if data == "[DONE]":
            return None
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise PayloadError(
                "Upstream stream event was not valid JSON.", status_code=502
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise PayloadError(
                "Upstream stream event has an invalid shape.", status_code=502
            )
        return event


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _encode_event(event: dict[str, Any]) -> bytes:
    return next(iter(encode_sse([event])))


def _terminal_failure_event(
    created: dict[str, Any] | None,
    *,
    sequence_number: int,
    message: str,
    code: str,
) -> dict[str, Any]:
    if created is None:
        response = response_object(
            {},
            "unknown",
            f"resp_{os.urandom(16).hex()}",
            "failed",
            [],
            None,
        )
    else:
        response = dict(created)
        response["status"] = "failed"
        response["completed_at"] = None
        response["output"] = []
        response["usage"] = None
    response["error"] = {"code": code, "message": message[:1_000]}
    return {
        "type": "response.failed",
        "sequence_number": sequence_number,
        "response": response,
    }


async def guarded_responses_sse(
    iterator: AsyncIterator[bytes],
    *,
    event_limit: int,
    heartbeat_seconds: float,
) -> AsyncIterator[bytes]:
    """Forward only complete typed events and always finish terminally.

    HTTP streaming starts with status 200 before an asynchronous provider can
    fail. Converting a late exception or truncated upstream event into a
    ``response.failed`` event prevents Codex from classifying it as a transport
    disconnect and retrying the turn as though no response had arrived.
    """

    upstream = aiter(iterator)
    decoder = SSEJSONDecoder(event_limit)
    pending: asyncio.Task[bytes] | None = None
    created: dict[str, Any] | None = None
    next_sequence = 0
    terminal = False
    failure: tuple[str, str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(upstream))
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield b": keep-alive\n\n"
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            for event in decoder.feed(chunk):
                sequence = event.get("sequence_number")
                if isinstance(sequence, int) and sequence >= next_sequence:
                    next_sequence = sequence + 1
                else:
                    next_sequence += 1
                if event.get("type") == "response.created" and isinstance(
                    event.get("response"), dict
                ):
                    created = event["response"]
                event_type = str(event["type"])
                yield _encode_event(event)
                if event_type in _TERMINAL_EVENTS:
                    terminal = True
                    return
        decoder.finish()
    except asyncio.CancelledError:
        raise
    except ProviderError as exc:
        failure = (str(exc), exc.error_type)
    except PayloadError as exc:
        failure = (str(exc), "upstream_protocol_error")
    except Exception as exc:  # pragma: no cover - defensive process boundary
        logger.exception(
            "Unexpected HTTP Responses stream failure error_type=%s",
            type(exc).__name__,
        )
        failure = (
            "Provider stream failed before a terminal Responses event.",
            "provider_stream_error",
        )
    finally:
        if pending is not None:
            await _cancel_task(pending)
        close = getattr(upstream, "aclose", None)
        if callable(close):
            await close()

    if terminal:
        return
    message, code = failure or (
        "Provider stream ended before a terminal Responses event.",
        "upstream_stream_incomplete",
    )
    yield _encode_event(
        _terminal_failure_event(
            created,
            sequence_number=next_sequence,
            message=message,
            code=code,
        )
    )


async def _websocket_json_body(websocket: WebSocket, limit: int) -> dict[str, Any]:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if not isinstance(text, str):
        raise PayloadError("WebSocket requests must be JSON text frames.")
    if len(text.encode("utf-8")) > limit:
        raise PayloadError("Request body is too large.", status_code=413)

    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        payload = json.loads(text, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PayloadError("Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PayloadError("Request body must be a JSON object.")
    return payload


def _input_items(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _restore_websocket_continuation(
    previous: _WebSocketResponseState,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild the logical request represented by an incremental WS turn."""
    restored = {**previous.request, **current}
    restored["input"] = [
        *_input_items(previous.request.get("input")),
        *previous.output,
        *_input_items(current.get("input")),
    ]
    for key in ("instructions", "tools"):
        if current.get(key) in (None, "", []) and key in previous.request:
            restored[key] = previous.request[key]
    restored.pop("generate", None)
    return restored


def validate_responses_body(body: dict[str, Any]) -> None:
    input_value = body.get("input")
    if isinstance(input_value, list) and len(input_value) > 10_000:
        raise PayloadError("input contains too many items.")
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise PayloadError("tools must be an array.")
        if len(tools) > 512:
            raise PayloadError("tools contains too many items.")
    metadata = body.get("client_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise PayloadError("client_metadata must be an object.")


def _validate_reconstructed_body(body: dict[str, Any], limit: int) -> None:
    validate_responses_body(body)
    try:
        size = len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise PayloadError("Reconstructed WebSocket request is invalid.") from exc
    if size > limit:
        raise PayloadError(
            "Reconstructed WebSocket request is too large.", status_code=413
        )


@dataclass(frozen=True, slots=True)
class _WebSocketResponseState:
    response_id: str
    request: dict[str, Any]
    output: list[Any]
    stream_id: str | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _QueuedResponse:
    body: dict[str, Any]
    stream_id: str | None
    provider_id: str


@dataclass(slots=True)
class _Lane:
    stream_id: str | None
    queue: asyncio.Queue[_QueuedResponse]
    worker: asyncio.Task[None] | None = None
    active: asyncio.Task[None] | None = None


class ResponsesWebSocketConnection:
    """Connection-local Responses WebSocket scheduler.

    A lane is FIFO and never self-overlaps. Named lanes can run concurrently,
    while a connection-wide semaphore implements the OpenAI 16-response active
    limit. The response cache is connection-local and preserves lineage
    independently from lane routing.
    """

    def __init__(
        self,
        websocket: WebSocket,
        service: _StreamingService,
        settings: AppSettings,
    ) -> None:
        self.websocket = websocket
        self.service = service
        self.settings = settings
        self._transport_headers = websocket.headers
        self.event_limit = max(
            settings.max_request_bytes,
            settings.kiro_max_output_bytes,
            settings.cursor_max_output_bytes,
            settings.direct_max_output_bytes,
        )
        self._send_lock = asyncio.Lock()
        self._transport_closed = asyncio.Event()
        self._closing = False
        self._capacity = asyncio.Semaphore(_MAX_ACTIVE_RESPONSES)
        self._active_count = 0
        self._queued_count = 0
        self._lanes: dict[str | None, _Lane] = {}
        self._named_streams: set[str] = set()
        self._responses: OrderedDict[str, _WebSocketResponseState] = OrderedDict()
        self._cache_bytes = 0
        self._cache_budget = min(
            64 * 1_048_576,
            max(settings.max_request_bytes, settings.max_request_bytes * 4),
        )

    async def run(self) -> None:
        try:
            while not self._transport_closed.is_set():
                try:
                    body = await self._receive()
                except PayloadError as exc:
                    await self._send_error(
                        str(exc),
                        "invalid_request_error",
                        status_code=exc.status_code,
                    )
                    continue
                await self._dispatch(body)
        except (WebSocketDisconnect, _ClientDisconnected):
            pass
        finally:
            await self._shutdown()

    async def _receive(self) -> dict[str, Any]:
        receive = asyncio.create_task(
            _websocket_json_body(self.websocket, self.settings.max_request_bytes)
        )
        closed = asyncio.create_task(self._transport_closed.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, closed}, return_when=asyncio.FIRST_COMPLETED
            )
            # Prefer the receive result when both tasks finish together. That
            # consumes WebSocketDisconnect instead of leaving an unobserved
            # child-task exception during ASGI connection teardown.
            if receive in done:
                return receive.result()
            raise _ClientDisconnected
        finally:
            await _cancel_task(receive)
            await _cancel_task(closed)

    async def _dispatch(self, value: dict[str, Any]) -> None:
        body = dict(value)
        event_type = body.pop("type", None)
        raw_stream_id = body.pop("stream_id", None)
        stream_id = await self._validated_stream_id(raw_stream_id)
        if raw_stream_id is not None and stream_id is None:
            return

        if event_type == "response.cancel":
            lane = self._lanes.get(stream_id)
            if lane is not None and lane.active is not None:
                logger.info(
                    "Cancelling active WebSocket response reason=client_cancel lane=%s",
                    self._lane_label(stream_id),
                )
                lane.active.cancel()
            return
        if event_type != "response.create":
            await self._send_error(
                "Unsupported WebSocket event type.",
                "invalid_request_error",
                status_code=400,
                code="invalid_event",
                stream_id=stream_id,
            )
            return
        body = bind_transport_context(body, self._transport_headers)
        if body.get("stream") not in (None, True):
            await self._send_error(
                "WebSocket response.create requires streaming mode.",
                "invalid_request_error",
                status_code=400,
                code="invalid_stream_value",
                param="stream",
                stream_id=stream_id,
            )
            return
        if body.get("background") not in (None, False):
            await self._send_error(
                "background is not supported in WebSocket mode.",
                "invalid_request_error",
                status_code=400,
                code="unsupported_background",
                param="background",
                stream_id=stream_id,
            )
            return
        body["stream"] = True
        try:
            validate_responses_body(body)
        except PayloadError as exc:
            await self._send_error(
                str(exc),
                "invalid_request_error",
                status_code=exc.status_code,
                stream_id=stream_id,
            )
            return
        if self._queued_count >= _MAX_QUEUED_RESPONSES:
            await self._send_error(
                "Responses WebSocket queue limit reached.",
                "invalid_request_error",
                status_code=429,
                code="websocket_queue_limit_reached",
                stream_id=stream_id,
            )
            return

        lane = self._lane(stream_id)
        provider_id = self.service.active_provider_id()
        await lane.queue.put(
            _QueuedResponse(
                body=body,
                stream_id=stream_id,
                provider_id=provider_id,
            )
        )
        self._queued_count += 1
        if lane.worker is None or lane.worker.done():
            lane.worker = asyncio.create_task(
                self._run_lane(lane),
                name=f"switchboard-ws-lane-{self._lane_label(stream_id)}",
            )

    async def _validated_stream_id(self, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _STREAM_ID.fullmatch(value) is None:
            await self._send_error(
                "The 'stream_id' field must be a non-empty string with at most "
                "256 characters and may only contain letters, numbers, "
                "underscores, hyphens, and periods.",
                "invalid_request_error",
                status_code=400,
                code="invalid_stream_id",
                param="stream_id",
            )
            return None
        if value not in self._named_streams:
            if len(self._named_streams) >= _MAX_NAMED_STREAMS:
                await self._send_error(
                    "This WebSocket connection has reached its maximum number "
                    "of distinct stream IDs (32).",
                    "invalid_request_error",
                    status_code=400,
                    code="websocket_stream_limit_reached",
                    param="stream_id",
                    stream_id=value,
                )
                return None
            self._named_streams.add(value)
        return value

    def _lane(self, stream_id: str | None) -> _Lane:
        lane = self._lanes.get(stream_id)
        if lane is None:
            lane = _Lane(stream_id=stream_id, queue=asyncio.Queue())
            self._lanes[stream_id] = lane
        return lane

    async def _run_lane(self, lane: _Lane) -> None:
        while not self._closing:
            request = await lane.queue.get()
            self._queued_count -= 1
            task = asyncio.create_task(
                self._run_with_capacity(request),
                name="switchboard-websocket-response",
            )
            lane.active = task
            try:
                await task
            except asyncio.CancelledError:
                if self._closing or asyncio.current_task().cancelling():
                    raise
            except _ClientDisconnected:
                self._transport_closed.set()
                return
            finally:
                lane.active = None
                lane.queue.task_done()

    async def _run_with_capacity(self, queued: _QueuedResponse) -> None:
        async with self._capacity:
            self._active_count += 1
            try:
                await self._process(queued)
            finally:
                self._active_count -= 1

    async def _process(self, queued: _QueuedResponse) -> None:
        body = dict(queued.body)
        stream_id = queued.stream_id
        previous_id = body.get("previous_response_id")
        previous: _WebSocketResponseState | None = None
        if previous_id is not None and not isinstance(previous_id, str):
            await self._send_error(
                "previous_response_id must be a string.",
                "invalid_request_error",
                status_code=400,
                param="previous_response_id",
                stream_id=stream_id,
            )
            return
        if isinstance(previous_id, str):
            previous = self._responses.get(previous_id)
            if previous is None:
                await self._send_error(
                    f"Previous response with id '{previous_id}' was not found.",
                    "invalid_request_error",
                    status_code=400,
                    code="previous_response_not_found",
                    param="previous_response_id",
                    stream_id=stream_id,
                )
                return
            self._responses.move_to_end(previous_id)
            body = _restore_websocket_continuation(previous, body)
            try:
                _validate_reconstructed_body(body, self.settings.max_request_bytes)
            except PayloadError as exc:
                await self._send_error(
                    str(exc),
                    "invalid_request_error",
                    status_code=exc.status_code,
                    stream_id=stream_id,
                )
                return

        if queued.body.get("generate") is False:
            state = await self._send_prewarm(body, stream_id)
            self._remember(state)
            return
        if "input" not in body:
            await self._send_error(
                "Missing required field: input",
                "invalid_request_error",
                status_code=400,
                stream_id=stream_id,
            )
            return

        thread_hash = codex_thread_key_hash(body)
        logger.info(
            "WebSocket response started provider=%s lane=%s thread=%s",
            queued.provider_id,
            self._lane_label(stream_id),
            thread_hash[:12] if thread_hash else "none",
        )
        try:
            state, terminal = await self._forward(
                queued.provider_id,
                body,
                stream_id,
            )
        except ProviderError as exc:
            await self._send_error(
                str(exc),
                exc.error_type,
                status_code=exc.status_code,
                stream_id=stream_id,
            )
            return
        except PayloadError as exc:
            await self._send_error(
                str(exc),
                "upstream_protocol_error",
                status_code=exc.status_code,
                stream_id=stream_id,
            )
            return
        except asyncio.CancelledError:
            raise
        except _ClientDisconnected:
            raise
        except Exception as exc:  # pragma: no cover - defensive process boundary
            logger.exception(
                "Unexpected WebSocket response failure error_type=%s",
                type(exc).__name__,
            )
            await self._send_error(
                "Provider stream failed before a terminal Responses event.",
                "provider_stream_error",
                status_code=502,
                stream_id=stream_id,
            )
            return
        if state is not None:
            self._remember(state)
        logger.info(
            "WebSocket response finished provider=%s lane=%s terminal=%s",
            queued.provider_id,
            self._lane_label(stream_id),
            terminal,
        )

    async def _forward(
        self,
        provider_id: str,
        request_body: dict[str, Any],
        stream_id: str | None,
    ) -> tuple[_WebSocketResponseState | None, str]:
        iterator = self.service.stream_for(provider_id, request_body)
        decoder = SSEJSONDecoder(self.event_limit)
        created: dict[str, Any] | None = None
        next_sequence = 0
        try:
            async for chunk in iterator:
                for event in decoder.feed(chunk):
                    event_type = str(event["type"])
                    sequence = event.get("sequence_number")
                    if isinstance(sequence, int) and sequence >= next_sequence:
                        next_sequence = sequence + 1
                    else:
                        next_sequence += 1
                    if event_type == "response.created" and isinstance(
                        event.get("response"), dict
                    ):
                        created = event["response"]
                    state: _WebSocketResponseState | None = None
                    if event_type == "response.completed":
                        response = event.get("response")
                        if isinstance(response, dict):
                            response_id = response.get("id")
                            output = response.get("output")
                            if isinstance(response_id, str) and isinstance(
                                output, list
                            ):
                                state = self._response_state(
                                    response_id,
                                    request_body,
                                    output,
                                    stream_id,
                                )
                    await self._send_event(event, stream_id)
                    if event_type in _TERMINAL_EVENTS:
                        return state, event_type
            decoder.finish()
        except ProviderError as exc:
            if created is None:
                raise
            await self._send_event(
                _terminal_failure_event(
                    created,
                    sequence_number=next_sequence,
                    message=str(exc),
                    code=exc.error_type,
                ),
                stream_id,
            )
            return None, "response.failed"
        except PayloadError as exc:
            if created is None:
                raise
            await self._send_event(
                _terminal_failure_event(
                    created,
                    sequence_number=next_sequence,
                    message=str(exc),
                    code="upstream_protocol_error",
                ),
                stream_id,
            )
            return None, "response.failed"
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()
        if created is None:
            raise PayloadError(
                "Provider stream ended before a terminal Responses event.",
                status_code=502,
            )
        await self._send_event(
            _terminal_failure_event(
                created,
                sequence_number=next_sequence,
                message="Provider stream ended before a terminal Responses event.",
                code="upstream_stream_incomplete",
            ),
            stream_id,
        )
        return None, "response.failed"

    async def _send_prewarm(
        self,
        body: dict[str, Any],
        stream_id: str | None,
    ) -> _WebSocketResponseState:
        response_id = f"resp_{os.urandom(16).hex()}"
        model_value = body.get("model")
        model = model_value if isinstance(model_value, str) else "unknown"
        response_body = {
            "model": model,
            "previous_response_id": body.get("previous_response_id"),
        }
        created = response_object(
            response_body, model, response_id, "in_progress", [], None
        )
        completed = response_object(
            response_body,
            model,
            response_id,
            "completed",
            [],
            {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 0,
            },
        )
        completed["created_at"] = created["created_at"]
        for event in (
            {"type": "response.created", "sequence_number": 0, "response": created},
            {
                "type": "response.in_progress",
                "sequence_number": 1,
                "response": created,
            },
            {
                "type": "response.completed",
                "sequence_number": 2,
                "response": completed,
            },
        ):
            await self._send_event(event, stream_id)
        return self._response_state(response_id, body, [], stream_id)

    def _response_state(
        self,
        response_id: str,
        request: dict[str, Any],
        output: list[Any],
        stream_id: str | None,
    ) -> _WebSocketResponseState:
        try:
            size = len(
                json.dumps(
                    {"request": request, "output": output},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError):
            size = self._cache_budget + 1
        return _WebSocketResponseState(
            response_id=response_id,
            request=dict(request),
            output=list(output),
            stream_id=stream_id,
            size_bytes=size,
        )

    def _remember(self, state: _WebSocketResponseState) -> None:
        if state.size_bytes > self.settings.max_request_bytes:
            logger.info(
                "WebSocket response state not cached reason=state_too_large lane=%s",
                self._lane_label(state.stream_id),
            )
            return
        self._evict(state.response_id)
        self._responses[state.response_id] = state
        self._cache_bytes += state.size_bytes
        while (
            len(self._responses) > _MAX_CACHED_RESPONSES
            or self._cache_bytes > self._cache_budget
        ):
            response_id, oldest = self._responses.popitem(last=False)
            self._cache_bytes -= oldest.size_bytes
            logger.debug(
                "Evicted WebSocket response state response=%s",
                hashlib.sha256(response_id.encode("utf-8")).hexdigest()[:12],
            )

    def _evict(self, response_id: str) -> None:
        state = self._responses.pop(response_id, None)
        if state is not None:
            self._cache_bytes -= state.size_bytes

    async def _send_event(self, event: dict[str, Any], stream_id: str | None) -> None:
        value = dict(event)
        if stream_id is None:
            value.pop("stream_id", None)
        else:
            value["stream_id"] = stream_id
        await self._send_json(value)

    async def _send_error(
        self,
        message: str,
        error_type: str,
        *,
        status_code: int,
        code: str | None = None,
        param: str | None = None,
        stream_id: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "error",
            "status": status_code,
            "error": {
                "message": message[:1_000],
                "type": error_type,
                "param": param,
                "code": code,
            },
        }
        if stream_id is not None:
            event["stream_id"] = stream_id
        await self._send_json(event)

    async def _send_json(self, value: dict[str, Any]) -> None:
        if self._transport_closed.is_set():
            raise _ClientDisconnected
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            if self._transport_closed.is_set():
                raise _ClientDisconnected
            try:
                await self.websocket.send_text(data)
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                self._transport_closed.set()
                raise _ClientDisconnected from exc

    async def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._transport_closed.set()
        active = sum(
            lane.active is not None and not lane.active.done()
            for lane in self._lanes.values()
        )
        if active or self._queued_count:
            logger.info(
                "WebSocket disconnected; cancelling responses active=%d queued=%d",
                active,
                self._queued_count,
            )
        workers = [
            lane.worker for lane in self._lanes.values() if lane.worker is not None
        ]
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    @staticmethod
    def _lane_label(stream_id: str | None) -> str:
        if stream_id is None:
            return "default"
        return hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:12]


async def run_responses_websocket(
    websocket: WebSocket,
    service: _StreamingService,
    settings: AppSettings,
) -> None:
    await ResponsesWebSocketConnection(websocket, service, settings).run()
