"""PostgreSQL JobQueue: FOR UPDATE SKIP LOCKED claiming, leases, fencing, retries."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from hive_mcp.adapters.postgres.db import lock
from hive_mcp.ports.jobs import Job, JobState

if TYPE_CHECKING:
    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.clock import Clock

MAX_SKIPS = 50  # dead jobs skipped in one claim


def _job(r: dict[str, Any]) -> Job:
    return Job(
        id=r["id"],
        org_id=r["org_id"],
        kind=r["kind"],
        payload=dict(r["payload"]),
        state=JobState(r["state"]),
        attempts=r["attempts"],
        max_attempts=r["max_attempts"],
        run_after=r["run_after"],
        dedupe_key=r["dedupe_key"],
        lease_owner=r["lease_owner"],
        lease_expires=r["lease_expires"],
        last_error=r["last_error"],
    )


class PgJobs:
    def __init__(self, db: PgDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

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
        now = self._clock.now()
        async with self._db.tx() as conn:
            if dedupe_key:
                await lock(conn, "job", org_id, kind, dedupe_key)
                cur = await conn.execute(
                    "SELECT * FROM jobs WHERE org_id=%s AND kind=%s AND dedupe_key=%s "
                    "AND state IN ('queued','running') ORDER BY seq LIMIT 1",
                    (org_id, kind, dedupe_key),
                )
                live = await cur.fetchone()
                if live:
                    return _job(live)
            cur = await conn.execute(
                "INSERT INTO jobs (id, org_id, kind, payload, state, attempts, max_attempts, run_after, "
                "dedupe_key) VALUES (%s,%s,%s,%s,'queued',0,%s,%s,%s) RETURNING *",
                (
                    uuid.uuid4().hex,
                    org_id,
                    kind,
                    Jsonb(payload),
                    max_attempts,
                    now + timedelta(seconds=delay_seconds),
                    dedupe_key,
                ),
            )
            return _job(await cur.fetchone())  # type: ignore[arg-type]

    async def claim(
        self, worker_id: str, kinds: frozenset[str], lease_seconds: float
    ) -> Job | None:
        if not kinds:
            return None
        now = self._clock.now()
        async with self._db.tx() as conn:
            for _ in range(MAX_SKIPS):
                cur = await conn.execute(
                    "SELECT * FROM jobs WHERE kind = ANY(%s) AND ((state='queued' AND run_after <= %s) "
                    "OR (state='running' AND lease_expires <= %s)) ORDER BY run_after, seq "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (sorted(kinds), now, now),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                if row["attempts"] >= row["max_attempts"]:  # crashed workers used every attempt
                    await conn.execute(
                        "UPDATE jobs SET state='dead', last_error='lease expired' WHERE id=%s",
                        (row["id"],),
                    )
                    continue
                cur = await conn.execute(
                    "UPDATE jobs SET state='running', attempts=attempts+1, lease_owner=%s, lease_expires=%s "
                    "WHERE id=%s RETURNING *",
                    (worker_id, now + timedelta(seconds=lease_seconds), row["id"]),
                )
                return _job(await cur.fetchone())  # type: ignore[arg-type]
        return None

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool:
        expires = self._clock.now() + timedelta(seconds=lease_seconds)
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET lease_expires=%s WHERE id=%s AND state='running' AND lease_owner=%s",
                (expires, job_id, worker_id),
            )
            return cur.rowcount == 1

    async def complete(self, job_id: str, worker_id: str) -> bool:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET state='done', lease_owner=NULL, lease_expires=NULL "
                "WHERE id=%s AND state='running' AND lease_owner=%s",
                (job_id, worker_id),
            )
            return cur.rowcount == 1

    async def fail(self, job_id: str, worker_id: str, error: str, retry_in_seconds: float) -> bool:
        retry_at = self._clock.now() + timedelta(seconds=retry_in_seconds)
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END, "
                "last_error=%s, lease_owner=NULL, lease_expires=NULL, "
                "run_after = CASE WHEN attempts >= max_attempts THEN run_after ELSE %s END "
                "WHERE id=%s AND state='running' AND lease_owner=%s",
                (error, retry_at, job_id, worker_id),
            )
            return cur.rowcount == 1

    async def get(self, job_id: str) -> Job | None:
        async with self._db.tx() as conn:
            cur = await conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            row = await cur.fetchone()
            return _job(row) if row else None
