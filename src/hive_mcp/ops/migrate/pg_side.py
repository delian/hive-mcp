"""PostgreSQL as a migration source (read-only transaction) or target (empty, schema created)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hive_mcp.adapters.postgres import PostgresBackend
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops.migrate.tables import BY_NAME, Row, Table

if TYPE_CHECKING:
    from collections.abc import Iterator


class PgSide:
    kind = "postgres"

    def __init__(self, dsn: str, *, target: bool) -> None:
        self._dsn = dsn
        if target:  # the schema (and the vector extension) is created exactly as the server does
            asyncio.run(self._create_schema(dsn))
        self._conn: psycopg.Connection[Any] = psycopg.connect(dsn, row_factory=dict_row)
        self._conn.execute("SET TimeZone = 'UTC'")
        self._conn.commit()
        if not target:
            self._conn.read_only = True  # a read-only transaction: it cannot change the source

    @staticmethod
    async def _create_schema(dsn: str) -> None:
        backend = await PostgresBackend.connect(dsn)
        await backend.close()

    def describe(self) -> str:
        return "PostgreSQL " + psycopg.conninfo.make_conninfo(self._dsn, password=None)[:80]

    def count(self, table: str) -> int:
        row = self._conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
        return int(row["n"]) if row else 0

    def is_empty(self) -> bool:
        return not any(self.count(t) for t in (*BY_NAME, "blobs"))

    def orgs(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT org_id FROM items ORDER BY 1").fetchall()
        return [r["org_id"] for r in rows]

    def read(self, table: Table) -> Iterator[Row]:
        columns = ", ".join(table.columns)
        with self._conn.cursor(name=f"migrate_{table.name}") as cursor:
            cursor.execute(f"SELECT {columns} FROM {table.name}")  # noqa: S608
            for r in cursor:
                row: Row = dict(r)
                if table.lists:
                    row["tags"] = sorted(row["tags"])
                yield row

    def blobs(self) -> Iterator[tuple[str, str, bytes]]:
        with self._conn.cursor(name="migrate_blobs") as cursor:
            cursor.execute("SELECT org_id, sha256, data FROM blobs")
            for r in cursor:
                yield r["org_id"], r["sha256"], bytes(r["data"])

    def write(self, table: Table, rows: list[Row]) -> None:
        columns = ", ".join(table.columns)
        marks = ", ".join(["%s"] * len(table.columns))
        for row in rows:
            values = [Jsonb(row[c]) if c in table.jsons else row[c] for c in table.columns]
            sql = f"INSERT INTO {table.name} ({columns}) VALUES ({marks})"  # noqa: S608
            self._conn.execute(sql, values)
        if table.name == "audit":  # the sequence continues after the copied events
            self._conn.execute(
                "SELECT setval(pg_get_serial_sequence('audit', 'seq'), "
                "(SELECT coalesce(max(seq), 1) FROM audit))"
            )
        self._conn.commit()

    def write_blob(self, org: str, sha: str, data: bytes) -> None:
        self._conn.execute(
            "INSERT INTO blobs (org_id, sha256, size, data) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (org, sha, len(data), data),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def require_vector_extension(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        if found.fetchone() is None:
            raise ValidationError("the Postgres server has no pgvector extension installed")
