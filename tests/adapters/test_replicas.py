"""Several server processes on one database: shared limits, shared blobs, safe settings."""

import asyncio
import os
import uuid

import psycopg
import pytest
import pytest_asyncio

from hive_mcp.adapters.postgres import PostgresBackend
from hive_mcp.adapters.postgres.ratelimit import PgRateLimiter
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.errors import StorageBusy

DSN = os.environ.get("HIVE_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="HIVE_TEST_PG_DSN not set")


@pytest_asyncio.fixture
async def two_replicas():
    schema = "t_" + uuid.uuid4().hex[:12]
    a = await PostgresBackend.connect(DSN, schema=schema)
    b = await PostgresBackend.connect(DSN, schema=schema)
    yield a, b
    await a.close()
    await b.close()
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@needs_pg
async def test_the_limit_holds_across_replicas(two_replicas):
    a, b = two_replicas
    first = PgRateLimiter(lambda: a.db, per_minute=1, burst=4)
    second = PgRateLimiter(lambda: b.db, per_minute=1, burst=4)
    waits = [await (first if i % 2 else second).ahit("ip:1") for i in range(6)]
    assert waits[:4] == [0.0] * 4 and all(
        w > 0 for w in waits[4:]
    )  # 4 allowed in total, not 4 each
    assert await first.ahit("ip:2") == 0.0  # other keys are independent


@needs_pg
async def test_concurrent_hits_never_exceed_the_burst(two_replicas):
    a, b = two_replicas
    limiters = [PgRateLimiter(lambda: a.db, 0.001, 10), PgRateLimiter(lambda: b.db, 0.001, 10)]
    waits = await asyncio.gather(*(limiters[i % 2].ahit("k") for i in range(40)))
    assert sum(1 for w in waits if w == 0.0) == 10


@needs_pg
async def test_buckets_refill_with_time(two_replicas):
    a, _ = two_replicas
    limiter = PgRateLimiter(lambda: a.db, per_minute=6000, burst=1)  # 100 per second
    assert await limiter.ahit("k") == 0.0
    assert await limiter.ahit("k") > 0
    await asyncio.sleep(0.05)
    assert await limiter.ahit("k") == 0.0


async def test_an_unavailable_database_does_not_block_requests():
    def down():
        raise StorageBusy("starting")

    assert await PgRateLimiter(down, 1, 1).ahit("k") == 0.0


@needs_pg
async def test_blobs_written_by_one_replica_are_read_by_the_other(two_replicas):
    a, b = two_replicas
    ref = await a.blobs.put("org1", b"session log")
    assert await b.blobs.get(ref) == b"session log"
    await b.blobs.delete(ref)
    assert not await a.blobs.exists(ref)


@needs_pg
async def test_idle_buckets_are_cleaned_up(two_replicas):
    a, _ = two_replicas
    await PgRateLimiter(lambda: a.db, 60, 5).ahit("old")
    async with a.db.tx() as conn:
        await conn.execute("UPDATE rate_buckets SET seen = seen - interval '2 hours'")
    await a.kv.purge_expired()
    async with a.db.tx() as conn:
        cur = await conn.execute("SELECT count(*) AS n FROM rate_buckets")
        assert (await cur.fetchone())["n"] == 0


def test_several_replicas_need_the_external_profile_and_a_fixed_key():
    with pytest.raises(ValueError, match="external profile"):
        Settings(replicas=2, secret_key="k")
    with pytest.raises(ValueError, match="HIVE_SECRET_KEY"):
        Settings(replicas=2, profile=Profile.EXTERNAL, database_url="x")
    assert (
        Settings(replicas=2, profile=Profile.EXTERNAL, database_url="x", secret_key="k").replicas
        == 2
    )
    assert Settings().replicas == 1
