import asyncio

import pytest

from codex_provider_switchboard.infrastructure.scheduler import (
    CapacityTimeoutError,
    FairCapacityScheduler,
)


def test_scheduler_is_fifo_and_releases_capacity() -> None:
    async def scenario() -> None:
        scheduler = FairCapacityScheduler(1, queue_timeout_seconds=1)
        first = await scheduler.acquire()
        order: list[int] = []

        async def worker(value: int) -> None:
            async with await scheduler.acquire():
                order.append(value)

        second = asyncio.create_task(worker(2))
        await asyncio.sleep(0)
        third = asyncio.create_task(worker(3))
        await asyncio.sleep(0)
        assert scheduler.snapshot().waiting == 2
        await first.release()
        await asyncio.gather(second, third)
        assert order == [2, 3]
        assert scheduler.snapshot().active == 0

    asyncio.run(scenario())


def test_scheduler_queue_deadline_is_bounded() -> None:
    async def scenario() -> None:
        scheduler = FairCapacityScheduler(1, queue_timeout_seconds=0.01)
        lease = await scheduler.acquire()
        try:
            with pytest.raises(CapacityTimeoutError):
                await scheduler.acquire()
        finally:
            await lease.release()
        assert scheduler.snapshot().active == 0
        assert scheduler.snapshot().waiting == 0

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_consume_capacity() -> None:
    async def scenario() -> None:
        scheduler = FairCapacityScheduler(1, queue_timeout_seconds=1)
        first = await scheduler.acquire()
        waiting = asyncio.create_task(scheduler.acquire())
        await asyncio.sleep(0)
        assert scheduler.snapshot().waiting == 1

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert scheduler.snapshot().waiting == 0

        await first.release()
        replacement = await asyncio.wait_for(scheduler.acquire(), timeout=0.2)
        assert scheduler.snapshot().active == 1
        await replacement.release()
        assert scheduler.snapshot().active == 0

    asyncio.run(scenario())
