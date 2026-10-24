"""SQLite KeyValueStore. Expiry is compared in Python with the injected clock (testable, no SQL time)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import ts

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import JsonObject


class SqliteKv:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db, self._clock = db, clock

    def _expiry(self, ttl_seconds: float | None) -> str | None:
        return (
            None if ttl_seconds is None else ts(self._clock.now() + timedelta(seconds=ttl_seconds))
        )

    def _live_row(self, conn: sqlite3.Connection, namespace: str, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = conn.execute(
            "SELECT value, expires_at FROM kv WHERE namespace = ? AND key = ?", (namespace, key)
        ).fetchone()
        if (
            row is not None
            and row["expires_at"] is not None
            and row["expires_at"] <= ts(self._clock.now())
        ):
            conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
            return None
        return row

    async def put(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> None:
        expires = self._expiry(ttl_seconds)

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO kv (namespace, key, value, expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
                (namespace, key, json.dumps(value), expires),
            )

        await self._db.run(tx, write=True)

    async def put_if_absent(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> bool:
        expires = self._expiry(ttl_seconds)

        def tx(conn: sqlite3.Connection) -> bool:
            if self._live_row(conn, namespace, key) is not None:
                return False
            conn.execute(
                "INSERT INTO kv (namespace, key, value, expires_at) VALUES (?,?,?,?)",
                (namespace, key, json.dumps(value), expires),
            )
            return True

        return await self._db.run(tx, write=True)

    async def get(self, namespace: str, key: str) -> JsonObject | None:
        def tx(conn: sqlite3.Connection) -> JsonObject | None:
            row = self._live_row(conn, namespace, key)
            return json.loads(row["value"]) if row else None

        return await self._db.run(tx, write=True)  # may delete an expired row

    async def take(self, namespace: str, key: str) -> JsonObject | None:
        def tx(conn: sqlite3.Connection) -> JsonObject | None:
            row = self._live_row(conn, namespace, key)
            conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
            return json.loads(row["value"]) if row else None

        return await self._db.run(tx, write=True)

    async def delete(self, namespace: str, key: str) -> None:
        await self._db.run(
            lambda c: c.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key)),
            write=True,
        )

    async def purge_expired(self) -> int:
        now = ts(self._clock.now())

        def tx(conn: sqlite3.Connection) -> int:
            return conn.execute(
                "DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            ).rowcount

        return await self._db.run(tx, write=True)
