"""Background job runner: claims durable jobs, runs handlers, retries with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import StorageBusy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from hive_mcp.ports.jobs import Job, JobQueue

log = logging.getLogger(__name__)
BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 3600.0


def backoff_seconds(attempts: int) -> float:
    return float(min(BASE_BACKOFF_SECONDS * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS))


class JobRunner:
    def __init__(
        self,
        jobs: JobQueue,
        handlers: Mapping[str, Callable[[Job], Awaitable[None]]],
        *,
        worker_id: str,
        lease_seconds: float = 120.0,
        poll_seconds: float = 2.0,
    ) -> None:
        self._jobs, self._handlers = jobs, handlers
        self._worker, self._lease, self._poll = worker_id, lease_seconds, poll_seconds

    async def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was claimed."""
        job = await self._jobs.claim(self._worker, frozenset(self._handlers), self._lease)
        if job is None:
            return False
        try:
            await self._handlers[job.kind](job)
        except asyncio.CancelledError:
            raise  # shutting down: the lease expires and another worker retries
        except Exception as exc:
            log.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
            await self._jobs.fail(
                job.id, self._worker, str(exc)[:500], backoff_seconds(job.attempts)
            )
        else:
            await self._jobs.complete(job.id, self._worker)
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                if not await self.run_once():
                    await asyncio.sleep(self._poll)
            except StorageBusy:
                await asyncio.sleep(self._poll)  # database hiccup: wait and keep going
