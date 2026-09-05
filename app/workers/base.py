"""Worker scaffolding: periodic loops that are safe to crash and restart.

Design rules every worker follows:

* All state lives in PostgreSQL. A worker holds nothing important in memory, so
  a crash loses at most one in-flight iteration.
* Each unit of work is idempotent, because it may be retried after a crash or
  picked up by a second replica.
* An exception in one item never aborts the batch: it is logged and the loop
  continues.
* Shutdown is cooperative: SIGTERM finishes the current item, then stops.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod

from app.core.correlation import correlation_scope
from app.core.logging import get_logger

log = get_logger(__name__)


class PeriodicWorker(ABC):
    """A loop that runs :meth:`run_once` every ``interval`` seconds."""

    name: str = "worker"
    interval: float = 30.0
    #: Random 0..jitter seconds added to each sleep so replicas desynchronise.
    jitter: float = 2.0

    def __init__(self, *, interval: float | None = None) -> None:
        if interval is not None:
            self.interval = interval
        self._stopping = asyncio.Event()
        self._consecutive_failures = 0

    @abstractmethod
    async def run_once(self) -> int:
        """Process one batch. Returns how many items were handled."""

    def stop(self) -> None:
        self._stopping.set()

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    async def run(self) -> None:
        log.info("worker.started", worker=self.name, interval=self.interval)
        while not self._stopping.is_set():
            with correlation_scope(prefix=f"w-{self.name}"):
                try:
                    processed = await self.run_once()
                    self._consecutive_failures = 0
                    if processed:
                        log.info("worker.batch", worker=self.name, processed=processed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._consecutive_failures += 1
                    log.exception(
                        "worker.iteration_failed",
                        worker=self.name,
                        consecutive_failures=self._consecutive_failures,
                    )
            await self._sleep()
        log.info("worker.stopped", worker=self.name)

    async def _sleep(self) -> None:
        """Sleep, backing off when the worker keeps failing."""
        delay = self.interval
        if self._consecutive_failures:
            delay = min(self.interval * (2 ** min(self._consecutive_failures, 5)), 300)
        delay += random.uniform(0, self.jitter)  # noqa: S311 - scheduling jitter only
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        except TimeoutError:
            pass


async def run_workers(workers: list[PeriodicWorker]) -> None:
    """Run workers concurrently until cancelled, then stop them cleanly."""
    tasks = [asyncio.create_task(worker.run(), name=worker.name) for worker in workers]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for worker in workers:
            worker.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
