from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from codex_provider_switchboard.web.app import _with_sse_heartbeat


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

        stream = _with_sse_heartbeat(silent(), interval_seconds=0.01)
        assert await asyncio.wait_for(anext(stream), timeout=0.5) == (
            b": keep-alive\n\n"
        )
        await stream.aclose()
        assert cancelled.is_set()
        assert closed.is_set()

    asyncio.run(scenario())


def test_sse_heartbeat_preserves_normal_events() -> None:
    events = [b"data: one\n\n", b"data: two\n\n"]

    async def scenario() -> list[bytes]:
        async def upstream() -> AsyncIterator[bytes]:
            for event in events:
                yield event

        return [
            chunk
            async for chunk in _with_sse_heartbeat(upstream(), interval_seconds=0.01)
        ]

    assert asyncio.run(scenario()) == events


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
            async for _ in _with_sse_heartbeat(silent(), interval_seconds=60.0):
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=0.5)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert cancelled.is_set()
        assert closed.is_set()

    asyncio.run(scenario())
