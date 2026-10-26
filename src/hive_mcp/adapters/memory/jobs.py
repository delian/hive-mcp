"""In-memory JobQueue with leases and fencing (same semantics every backend must provide)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.ports.jobs import Job, JobStat, JobState

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock

_LIVE = (JobState.QUEUED, JobState.RUNNING)


class MemoryJobs:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}  # insertion order = creation order

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
        if dedupe_key:
            for job in self._jobs.values():
                same = (job.org_id, job.kind, job.dedupe_key) == (org_id, kind, dedupe_key)
                if same and job.state in _LIVE:
                    return job
        now = self._clock.now()
        job = Job(
            id=uuid.uuid4().hex,
            org_id=org_id,
            kind=kind,
            payload=dict(payload),
            state=JobState.QUEUED,
            attempts=0,
            max_attempts=max_attempts,
            run_after=now + timedelta(seconds=delay_seconds),
            dedupe_key=dedupe_key,
        )
        self._jobs[job.id] = job
        return job

    async def claim(
        self, worker_id: str, kinds: frozenset[str], lease_seconds: float
    ) -> Job | None:
        now = self._clock.now()
        for job in sorted(self._jobs.values(), key=lambda j: j.run_after):
            if job.kind not in kinds:
                continue
            due = job.state is JobState.QUEUED and job.run_after <= now
            expired = job.state is JobState.RUNNING and (job.lease_expires or now) <= now
            if not (due or expired):
                continue
            if job.attempts >= job.max_attempts:  # crashed workers consumed every attempt
                self._jobs[job.id] = replace(job, state=JobState.DEAD, last_error="lease expired")
                continue
            claimed = replace(
                job,
                state=JobState.RUNNING,
                attempts=job.attempts + 1,
                lease_owner=worker_id,
                lease_expires=now + timedelta(seconds=lease_seconds),
            )
            self._jobs[job.id] = claimed
            return claimed
        return None

    def _held(self, job_id: str, worker_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job and job.state is JobState.RUNNING and job.lease_owner == worker_id:
            return job
        return None

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool:
        job = self._held(job_id, worker_id)
        if job is None:
            return False
        expires = self._clock.now() + timedelta(seconds=lease_seconds)
        self._jobs[job_id] = replace(job, lease_expires=expires)
        return True

    async def complete(self, job_id: str, worker_id: str) -> bool:
        job = self._held(job_id, worker_id)
        if job is None:
            return False
        self._jobs[job_id] = replace(job, state=JobState.DONE, lease_owner=None, lease_expires=None)
        return True

    async def fail(self, job_id: str, worker_id: str, error: str, retry_in_seconds: float) -> bool:
        job = self._held(job_id, worker_id)
        if job is None:
            return False
        dead = job.attempts >= job.max_attempts
        retry_at = self._clock.now() + timedelta(seconds=retry_in_seconds)
        self._jobs[job_id] = replace(
            job,
            state=JobState.DEAD if dead else JobState.QUEUED,
            last_error=error,
            lease_owner=None,
            lease_expires=None,
            run_after=job.run_after if dead else retry_at,
        )
        return True

    async def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def stats(self) -> list[JobStat]:
        now = self._clock.now()
        groups: dict[tuple[str, JobState], list[Job]] = {}
        for job in self._jobs.values():
            if job.state is not JobState.DONE:
                groups.setdefault((job.kind, job.state), []).append(job)
        out = []
        for (kind, state), jobs in sorted(groups.items()):
            due = [j.run_after for j in jobs if state is JobState.QUEUED and j.run_after <= now]
            age = (now - min(due)).total_seconds() if due else 0.0
            out.append(JobStat(kind, state, len(jobs), age))
        return out
