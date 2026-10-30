"""PostgreSQL KeyValueStore. `take` is a single DELETE ... RETURNING, atomic across processes."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import JsonObject


class PgKv:
    def __init__(self, db: PgDatabase, clock: Clock) -> None:
        self._db, self._clock = db, clock

    def _expiry(self, ttl_seconds: float | None) -> datetime | None:
        return None if ttl_seconds is None else self._clock.now() + timedelta(seconds=ttl_seconds)

    async def put(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO kv (namespace, key, value, expires_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
                (namespace, key, Jsonb(value), self._expiry(ttl_seconds)),
            )

    async def put_if_absent(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> bool:
        now = self._clock.now()
        async with self._db.tx() as conn:
            # An expired row must not block a new one; the conditional upsert replaces it atomically.
            cur = await conn.execute(
                "INSERT INTO kv (namespace, key, value, expires_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at "
                "WHERE kv.expires_at IS NOT NULL AND kv.expires_at <= %s",
                (namespace, key, Jsonb(value), self._expiry(ttl_seconds), now),
            )
            return cur.rowcount == 1

    async def get(self, namespace: str, key: str) -> JsonObject | None:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT value FROM kv WHERE namespace = %s AND key = %s "
                "AND (expires_at IS NULL OR expires_at > %s)",
                (namespace, key, self._clock.now()),
            )
            row = await cur.fetchone()
            return dict(row["value"]) if row else None

    async def take(self, namespace: str, key: str) -> JsonObject | None:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "DELETE FROM kv WHERE namespace = %s AND key = %s RETURNING value, expires_at",
                (namespace, key),
            )
            row = await cur.fetchone()
            if row is None or (
                row["expires_at"] is not None and row["expires_at"] <= self._clock.now()
            ):
                return None
            return dict(row["value"])

    async def delete(self, namespace: str, key: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute("DELETE FROM kv WHERE namespace = %s AND key = %s", (namespace, key))

    async def scan(
        self, namespace: str, prefix: str = "", limit: int = 100
    ) -> list[tuple[str, JsonObject]]:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM kv WHERE namespace = %s AND starts_with(key, %s) "
                'AND (expires_at IS NULL OR expires_at > %s) ORDER BY key COLLATE "C" LIMIT %s',
                (namespace, prefix, self._clock.now(), limit),
            )
            return [(row["key"], dict(row["value"])) for row in await cur.fetchall()]

    async def purge_expired(self) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= %s",
                (self._clock.now(),),
            )
            expired = cur.rowcount
            await conn.execute(  # idle rate-limit buckets have long since refilled
                "DELETE FROM rate_buckets WHERE seen < clock_timestamp() - interval '1 hour'"
            )
            return expired
