from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from codex_provider_switchboard.providers._streaming import (
    ResponseEventStream,
    with_response_heartbeat,
)


def _event(chunk: bytes) -> dict[str, object]:
    data = next(
        line[6:] for line in chunk.decode().splitlines() if line.startswith("data: ")
    )
    value = json.loads(data)
    assert isinstance(value, dict)
    return value


def test_response_heartbeat_is_a_real_event_with_monotonic_sequence() -> None:
    async def scenario() -> list[dict[str, object]]:
        events = ResponseEventStream(
            {"input": "hello"}, "gpt-test", error_code="test_error"
        )
        cancelled = asyncio.Event()

        async def silent() -> AsyncIterator[bytes]:
            for event in events.begin():
                yield event
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        stream = with_response_heartbeat(
            silent(), events.in_progress, interval_seconds=0.01
        )
        decoded = [_event(await asyncio.wait_for(anext(stream), timeout=0.5))]
        decoded.append(_event(await asyncio.wait_for(anext(stream), timeout=0.5)))
        decoded.append(_event(await asyncio.wait_for(anext(stream), timeout=0.5)))
        await stream.aclose()
        assert cancelled.is_set()
        return decoded

    decoded = asyncio.run(scenario())
    assert [event["type"] for event in decoded] == [
        "response.created",
        "response.in_progress",
        "response.in_progress",
    ]
    assert [event["sequence_number"] for event in decoded] == [0, 1, 2]
    assert decoded[2]["response"] == decoded[1]["response"]


def test_response_heartbeat_keeps_later_events_in_sequence() -> None:
    async def scenario() -> list[dict[str, object]]:
        events = ResponseEventStream(
            {"input": "hello"}, "gpt-test", error_code="test_error"
        )

        async def delayed_completion() -> AsyncIterator[bytes]:
            for event in events.begin():
                yield event
            await asyncio.sleep(0.025)
            yield events.completed([], None)

        return [
            _event(chunk)
            async for chunk in with_response_heartbeat(
                delayed_completion(), events.in_progress, interval_seconds=0.01
            )
        ]

    decoded = asyncio.run(scenario())
    assert decoded[-1]["type"] == "response.completed"
    assert any(event["type"] == "response.in_progress" for event in decoded[2:-1])
    assert [event["sequence_number"] for event in decoded] == list(range(len(decoded)))


def test_cancelling_response_heartbeat_cancels_upstream() -> None:
    async def scenario() -> None:
        events = ResponseEventStream(
            {"input": "hello"}, "gpt-test", error_code="test_error"
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def silent() -> AsyncIterator[bytes]:
            yield events.begin()[0]
            started.set()
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def consume() -> None:
            async for _ in with_response_heartbeat(
                silent(), events.in_progress, interval_seconds=60.0
            ):
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=0.5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert cancelled.is_set()

    asyncio.run(scenario())
