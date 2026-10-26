"""SQLite JobQueue: leases, fencing and retries, each operation one IMMEDIATE transaction."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.ports.jobs import Job, JobStat, JobState

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.clock import Clock


def _job(r: sqlite3.Row) -> Job:
    return Job(
        id=r["id"],
        org_id=r["org_id"],
        kind=r["kind"],
        payload=json.loads(r["payload"]),
        state=JobState(r["state"]),
        attempts=r["attempts"],
        max_attempts=r["max_attempts"],
        run_after=parse_ts(r["run_after"]),
        dedupe_key=r["dedupe_key"],
        lease_owner=r["lease_owner"],
        lease_expires=parse_ts(r["lease_expires"]) if r["lease_expires"] else None,
        last_error=r["last_error"],
    )


def _held(conn: sqlite3.Connection, job_id: str, worker_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND state = 'running' AND lease_owner = ?",
        (job_id, worker_id),
    ).fetchone()
    return row


class SqliteJobs:
    def __init__(self, db: Database, clock: Clock) -> None:
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

        def tx(conn: sqlite3.Connection) -> Job:
            if dedupe_key:
                live = conn.execute(
                    "SELECT * FROM jobs WHERE org_id=? AND kind=? AND dedupe_key=? "
                    "AND state IN ('queued','running') ORDER BY seq LIMIT 1",
                    (org_id, kind, dedupe_key),
                ).fetchone()
                if live:
                    return _job(live)
            job_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO jobs (id, org_id, kind, payload, state, attempts, max_attempts, run_after, dedupe_key) "
                "VALUES (?,?,?,?, 'queued', 0, ?, ?, ?)",
                (
                    job_id,
                    org_id,
                    kind,
                    json.dumps(payload),
                    max_attempts,
                    ts(now + timedelta(seconds=delay_seconds)),
                    dedupe_key,
                ),
            )
            return _job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

        return await self._db.run(tx, write=True)

    async def claim(
        self, worker_id: str, kinds: frozenset[str], lease_seconds: float
    ) -> Job | None:
        if not kinds:
            return None
        now = self._clock.now()

        def tx(conn: sqlite3.Connection) -> Job | None:
            marks = ",".join("?" * len(kinds))
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE kind IN ({marks}) AND "  # noqa: S608
                "((state='queued' AND run_after <= ?) OR (state='running' AND lease_expires <= ?)) "
                "ORDER BY run_after, seq",
                [*sorted(kinds), ts(now), ts(now)],
            ).fetchall()
            for row in rows:
                if row["attempts"] >= row["max_attempts"]:  # crashed workers consumed every attempt
                    conn.execute(
                        "UPDATE jobs SET state='dead', last_error='lease expired' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                conn.execute(
                    "UPDATE jobs SET state='running', attempts=attempts+1, lease_owner=?, lease_expires=? WHERE id=?",
                    (worker_id, ts(now + timedelta(seconds=lease_seconds)), row["id"]),
                )
                return _job(
                    conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
                )
            return None

        return await self._db.run(tx, write=True)

    async def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool:
        expires = ts(self._clock.now() + timedelta(seconds=lease_seconds))

        def tx(conn: sqlite3.Connection) -> bool:
            if not _held(conn, job_id, worker_id):
                return False
            conn.execute("UPDATE jobs SET lease_expires = ? WHERE id = ?", (expires, job_id))
            return True

        return await self._db.run(tx, write=True)

    async def complete(self, job_id: str, worker_id: str) -> bool:
        def tx(conn: sqlite3.Connection) -> bool:
            if not _held(conn, job_id, worker_id):
                return False
            conn.execute(
                "UPDATE jobs SET state='done', lease_owner=NULL, lease_expires=NULL WHERE id=?",
                (job_id,),
            )
            return True

        return await self._db.run(tx, write=True)

    async def fail(self, job_id: str, worker_id: str, error: str, retry_in_seconds: float) -> bool:
        retry_at = ts(self._clock.now() + timedelta(seconds=retry_in_seconds))

        def tx(conn: sqlite3.Connection) -> bool:
            row = _held(conn, job_id, worker_id)
            if not row:
                return False
            dead = row["attempts"] >= row["max_attempts"]
            conn.execute(
                "UPDATE jobs SET state=?, last_error=?, lease_owner=NULL, lease_expires=NULL, run_after=? WHERE id=?",
                (
                    "dead" if dead else "queued",
                    error,
                    row["run_after"] if dead else retry_at,
                    job_id,
                ),
            )
            return True

        return await self._db.run(tx, write=True)

    async def get(self, job_id: str) -> Job | None:
        def tx(conn: sqlite3.Connection) -> Job | None:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _job(row) if row else None

        return await self._db.run(tx)

    async def stats(self) -> list[JobStat]:
        now = self._clock.now()

        def tx(conn: sqlite3.Connection) -> list[JobStat]:
            rows = conn.execute(
                "SELECT kind, state, COUNT(*) AS n, "
                "MIN(CASE WHEN state='queued' AND run_after <= ? THEN run_after END) AS due "
                "FROM jobs WHERE state != 'done' GROUP BY kind, state ORDER BY kind, state",
                (ts(now),),
            ).fetchall()
            return [
                JobStat(
                    r["kind"],
                    JobState(r["state"]),
                    r["n"],
                    (now - parse_ts(r["due"])).total_seconds() if r["due"] else 0.0,
                )
                for r in rows
            ]

        return await self._db.run(tx)
