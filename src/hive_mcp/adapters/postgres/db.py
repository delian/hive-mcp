"""Connection pool wrapper: one transaction per operation, driver errors mapped to domain errors."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from hive_mcp.adapters.postgres.schema import migrate
from hive_mcp.domain.errors import StorageBusy, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


async def lock(conn: psycopg.AsyncConnection[Any], *parts: str) -> None:
    """Transaction-scoped advisory lock on a named resource. The key is a hash of the JSON-encoded parts,
    so ("a", "bc") and ("ab", "c") never collide and no separator character is needed."""
    digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (digest,))


class PgDatabase:
    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def open(
        cls,
        dsn: str,
        *,
        schema: str | None = None,
        max_size: int = 10,
        acquire_timeout: float = 10.0,
    ) -> PgDatabase:
        options = "-c TimeZone=UTC"
        if schema:
            if not _IDENT.match(schema):
                raise ValidationError(f"invalid schema name: {schema!r}")
            # "IF NOT EXISTS" alone is not safe when several instances start together: both pass the
            # check and one then fails on the catalog's unique index. A transaction-scoped lock serialises them.
            async with await psycopg.AsyncConnection.connect(dsn) as admin:
                await lock(admin, "create-schema", schema)
                await admin.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            options += f" -c search_path={schema},public"
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            timeout=acquire_timeout,
            open=False,
            kwargs={"row_factory": dict_row, "options": options},
        )
        await pool.open(wait=True, timeout=acquire_timeout)
        db = cls(pool)
        async with db.tx() as conn:
            await migrate(conn)
        return db

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        """One transaction: commits on success, rolls back on any exception."""
        try:
            async with self._pool.connection() as conn:
                yield conn
        except PoolTimeout as exc:
            raise StorageBusy("no database connection available, retry") from exc
        except psycopg.errors.DataError as exc:  # e.g. NUL bytes, out-of-range values
            raise ValidationError(f"invalid data: {exc}") from exc
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure) as exc:
            raise StorageBusy("transaction conflict, retry") from exc
        except psycopg.OperationalError as exc:
            raise StorageBusy("database unavailable, retry") from exc

    async def close(self) -> None:
        await self._pool.close()
