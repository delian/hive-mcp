"""Every backend registers a factory here; the whole suite then runs against it.

The Postgres backend needs a server with pgvector: set HIVE_TEST_PG_DSN (for example
"host=/tmp port=55432 dbname=hive_test user=postgres"). Without it those cases are skipped.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
import pytest_asyncio

from hive_mcp.adapters.memory import MemoryBackend
from hive_mcp.adapters.postgres import PostgresBackend
from hive_mcp.adapters.sqlite import SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

PG_DSN = os.environ.get("HIVE_TEST_PG_DSN")
BACKENDS = ["memory", "sqlite", "postgres"]


@pytest_asyncio.fixture(params=BACKENDS)
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield MemoryBackend()
    elif request.param == "sqlite":
        sqlite = SqliteBackend(tmp_path / "data")
        yield sqlite
        sqlite.close()
    else:
        if not PG_DSN:
            pytest.skip("HIVE_TEST_PG_DSN not set")
        schema = (
            "t_" + uuid.uuid4().hex[:12]
        )  # one schema per test: isolation without recreating the database
        pg = await PostgresBackend.connect(PG_DSN, schema=schema)
        yield pg
        await pg.close()
        async with await psycopg.AsyncConnection.connect(PG_DSN, autocommit=True) as admin:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
