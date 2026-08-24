from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from codex_provider_switchboard.web.responses_transport import (
    SSEJSONDecoder,
    guarded_responses_sse,
)


def _sse(event: dict[str, object]) -> bytes:
    data = json.dumps(event, separators=(",", ":"))
    return f"event: {event['type']}\ndata: {data}\n\n".encode()


def _events(chunks: list[bytes]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for chunk in chunks:
        data = [
            line[6:]
            for line in chunk.decode().splitlines()
            if line.startswith("data: ")
        ]
        if data:
            value = json.loads("\n".join(data))
            assert isinstance(value, dict)
            result.append(value)
    return result


def test_sse_decoder_bounds_each_record_not_a_batched_transport_chunk() -> None:
    first = {"type": "one"}
    second = {"type": "two"}
    first_wire = _sse(first)
    second_wire = _sse(second)
    limit = max(len(first_wire), len(second_wire))

    assert SSEJSONDecoder(limit).feed(first_wire + second_wire) == [first, second]


def test_sse_heartbeat_emits_comments_while_upstream_is_silent() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()
        closed = asyncio.Event()

        async def silent() -> AsyncIterator[bytes]:
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                closed.set()

        stream = guarded_responses_sse(
            silent(), event_limit=4_096, heartbeat_seconds=0.01
        )
        assert await asyncio.wait_for(anext(stream), timeout=0.5) == (
            b": keep-alive\n\n"
        )
        await stream.aclose()
        assert cancelled.is_set()
        assert closed.is_set()

    asyncio.run(scenario())


def test_sse_heartbeat_preserves_normal_events() -> None:
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": {
            "id": "resp_test",
            "object": "response",
            "status": "in_progress",
            "output": [],
        },
    }
    completed = {
        "type": "response.completed",
        "sequence_number": 1,
        "response": {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "output": [],
        },
    }
    chunks = [_sse(created), _sse(completed)]

    async def scenario() -> list[bytes]:
        async def upstream() -> AsyncIterator[bytes]:
            for event in chunks:
                yield event

        return [
            chunk
            async for chunk in guarded_responses_sse(
                upstream(), event_limit=4_096, heartbeat_seconds=0.01
            )
        ]

    assert _events(asyncio.run(scenario())) == [created, completed]


def test_cancelling_sse_consumer_cancels_and_closes_upstream() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        closed = asyncio.Event()

        async def silent() -> AsyncIterator[bytes]:
            started.set()
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                closed.set()

        async def consume() -> None:
            async for _ in guarded_responses_sse(
                silent(), event_limit=4_096, heartbeat_seconds=60.0
            ):
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=0.5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert cancelled.is_set()
        assert closed.is_set()

    asyncio.run(scenario())


def test_sse_eof_without_terminal_becomes_response_failed() -> None:
    async def scenario() -> list[dict[str, object]]:
        async def incomplete() -> AsyncIterator[bytes]:
            yield _sse(
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": {
                        "id": "resp_incomplete",
                        "object": "response",
                        "status": "in_progress",
                        "output": [],
                    },
                }
            )

        chunks = [
            chunk
            async for chunk in guarded_responses_sse(
                incomplete(), event_limit=4_096, heartbeat_seconds=0.01
            )
        ]
        return _events(chunks)

    events = asyncio.run(scenario())
    assert [event["type"] for event in events] == [
        "response.created",
        "response.failed",
    ]
    assert events[-1]["response"]["id"] == "resp_incomplete"  # type: ignore[index]
    assert events[-1]["response"]["error"]["code"] == (  # type: ignore[index]
        "upstream_stream_incomplete"
    )
