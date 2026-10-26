"""Durable background jobs with leases, so work survives restarts and crashed workers.

Rules: a claimed job is leased to one worker; if the lease expires another worker may reclaim it.
`complete`, `fail` and `heartbeat` are *fenced*: they only succeed for the current lease holder.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass field
from enum import StrEnum
from typing import Protocol


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"  # exhausted max_attempts


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    org_id: str
    kind: str
    payload: dict[str, str]
    state: JobState
    attempts: int
    max_attempts: int
    run_after: datetime
    dedupe_key: str | None = None
    lease_owner: str | None = None
    lease_expires: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class JobStat:
    """Backlog of one kind in one state; `oldest_due_age` (seconds) is for QUEUED jobs only."""

    kind: str
    state: JobState
    count: int
    oldest_due_age: float = 0.0


class JobQueue(Protocol):
    async def enqueue(  # noqa: PLR0913 - keyword-only options
        self,
        org_id: str,
        kind: str,
        payload: dict[str, str],
        *,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
        delay_seconds: float = 0,
    ) -> Job:
        """With a dedupe_key, returns the existing job while one is QUEUED or RUNNING."""

    async def claim(
        self, worker_id: str, kinds: frozenset[str], lease_seconds: float
    ) -> Job | None:
        """Oldest runnable job: QUEUED and due, or RUNNING with an expired lease (attempts += 1)."""

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool: ...

    async def complete(self, job_id: str, worker_id: str) -> bool: ...

    async def fail(self, job_id: str, worker_id: str, error: str, retry_in_seconds: float) -> bool:
        """Requeues after the delay, or marks DEAD once attempts reach max_attempts."""

    async def get(self, job_id: str) -> Job | None: ...

    async def stats(self) -> list[JobStat]:
        """Counts of QUEUED, RUNNING and DEAD jobs per kind, for monitoring (DONE is omitted)."""
