from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass


class CapacityTimeoutError(TimeoutError):
    """Raised when provider capacity is unavailable before its deadline."""


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    capacity: int
    active: int
    waiting: int


class ExecutionLease:
    """One capacity grant with idempotent release."""

    def __init__(self, scheduler: FairCapacityScheduler, queue_ms: int) -> None:
        self._scheduler = scheduler
        self.queue_ms = queue_ms
        self._released = False

    async def __aenter__(self) -> ExecutionLease:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._scheduler._release()


class FairCapacityScheduler:
    """FIFO, bounded-capacity scheduler for expensive provider work.

    State transitions are synchronous because every instance is event-loop local.
    This preserves FIFO ordering and prevents cancellation from consuming a grant
    between separate awaited bookkeeping operations.
    """

    def __init__(self, capacity: int, *, queue_timeout_seconds: float) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least one")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue timeout must be positive")
        self.capacity = capacity
        self.queue_timeout_seconds = queue_timeout_seconds
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    def _grant_waiters(self) -> None:
        while self._active < self.capacity and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self._active += 1
            waiter.set_result(None)

    async def acquire(self) -> ExecutionLease:
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._waiters.append(waiter)
        self._grant_waiters()
        try:
            await asyncio.wait_for(
                asyncio.shield(waiter), timeout=self.queue_timeout_seconds
            )
        except BaseException as exc:
            if waiter.done() and not waiter.cancelled():
                # The grant became active before timeout/cancellation was delivered.
                self._active -= 1
                self._grant_waiters()
            else:
                with suppress(ValueError):
                    self._waiters.remove(waiter)
                waiter.cancel()
            if isinstance(exc, TimeoutError):
                raise CapacityTimeoutError(
                    "Provider capacity queue deadline exceeded."
                ) from exc
            raise
        queue_ms = int((time.monotonic() - started) * 1_000)
        return ExecutionLease(self, queue_ms)

    def _release(self) -> None:
        if self._active < 1:
            raise RuntimeError("provider capacity lease released without acquire")
        self._active -= 1
        self._grant_waiters()

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(
            capacity=self.capacity,
            active=self._active,
            waiting=sum(not waiter.done() for waiter in self._waiters),
        )
