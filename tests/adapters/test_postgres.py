"""PostgreSQL-specific behaviour. Needs HIVE_TEST_PG_DSN (a server with pgvector)."""

import asyncio
import os
import uuid

import numpy as np
import psycopg
import pytest
import pytest_asyncio
from hypothesis import given, settings
from hypothesis import strategies as st

from hive_mcp.adapters.postgres import PostgresBackend
from hive_mcp.adapters.postgres.acl import acl_where
from hive_mcp.adapters.postgres.db import PgDatabase
from hive_mcp.adapters.postgres.schema import MIGRATIONS
from hive_mcp.adapters.postgres.text import tsquery_expression
from hive_mcp.adapters.postgres.vectors import MAX_DIM, table_for
from hive_mcp.domain.access import Acl, ScopeFilter
from hive_mcp.domain.errors import HiveError, StorageBusy, ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from tests.conformance.factory import ALICE, P1, make_item

DSN = os.environ.get("HIVE_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="HIVE_TEST_PG_DSN not set")


@pytest_asyncio.fixture
async def schema():
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


async def test_data_survives_reconnect(schema, tmp_path):
    first = await PostgresBackend.connect(DSN, schema=schema)
    item = make_item(first.clock)
    await first.items.create(ALICE, item, idempotency_key="k")
    await first.close()
    second = await PostgresBackend.connect(DSN, schema=schema)
    assert (await second.items.get(ALICE, item.id)).item == item
    assert not (await second.items.create(ALICE, item, idempotency_key="k")).created
    await second.close()


async def test_refuses_database_from_the_future(schema, tmp_path):
    first = await PostgresBackend.connect(DSN, schema=schema)
    await first.close()
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(
            f'INSERT INTO "{schema}".schema_migrations VALUES ({len(MIGRATIONS) + 4})'  # noqa: S608
        )
    with pytest.raises(HiveError, match="newer"):
        await PostgresBackend.connect(DSN, schema=schema)


async def test_simultaneous_app_starts_migrate_once(schema):
    # return_exceptions + finally: a failed start must not leave other pools open (can stall exit)
    results = await asyncio.gather(
        *(PgDatabase.open(DSN, schema=schema, max_size=2) for _ in range(6)), return_exceptions=True
    )
    opened = [r for r in results if isinstance(r, PgDatabase)]
    try:
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        async with opened[0].tx() as conn:
            cur = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            assert [r["version"] for r in await cur.fetchall()] == list(
                range(1, len(MIGRATIONS) + 1)
            )
    finally:
        await asyncio.gather(*(db.close() for db in opened))


async def test_pool_exhaustion_is_a_retryable_error(schema):
    db = await PgDatabase.open(DSN, schema=schema, max_size=1, acquire_timeout=0.3)
    async with db.tx() as held:
        await held.execute("SELECT 1")
        with pytest.raises(StorageBusy):
            async with db.tx() as other:
                await other.execute("SELECT 1")
    async with db.tx() as conn:  # the pool recovers once the connection is returned
        await conn.execute("SELECT 1")
    await db.close()


async def test_invalid_data_becomes_a_validation_error(schema, tmp_path):
    pg = await PostgresBackend.connect(DSN, schema=schema)
    with pytest.raises(ValidationError):
        await pg.jobs.enqueue("org1", "ingest", {"k": "nul\x00byte"})
    assert (await pg.jobs.enqueue("org1", "ingest", {"k": "fine"})).payload == {"k": "fine"}
    await pg.close()


async def test_vector_tables_per_model_with_hnsw_index(schema, tmp_path):
    pg = await PostgresBackend.connect(DSN, schema=schema)
    acl = Acl("org1", P1, Visibility.SCOPE)
    for model in ("m'one", 'm"two', "m;DROP TABLE items;--", "модел"):
        await pg.vectors.replace("a", model, acl, [np.array([1, 0, 0], dtype=np.float32)])
    async with pg.db.tx() as conn:
        cur = await conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = %s", (schema,))
        defs = [r["indexdef"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT count(*) AS n FROM vec_models")
        assert (await cur.fetchone())["n"] == 4
    assert sum("USING hnsw" in d and "halfvec_ip_ops" in d for d in defs) == 4
    assert len({table_for(m) for m in ("a", "b")}) == 2
    with pytest.raises(ValidationError):
        await pg.vectors.replace("a", "huge", acl, [np.ones(MAX_DIM + 1, dtype=np.float32)])
    await pg.close()


@pytest.mark.parametrize(
    "term", ["a'b", "x\\", "!", "&", "|", ":*", "(", ")", "<->", "a:b", "'", "''"]
)
async def test_tsquery_expression_neutralises_operators(schema, term):
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute("SELECT to_tsquery('simple', %s)", (tsquery_expression([term]),))


IDS = st.sampled_from(["a", "b", "c"])
ACLS = st.builds(
    lambda org, level, owner, vis: Acl(
        org, Scope(level, owner), Visibility.SCOPE if level is ScopeLevel.PRIVATE else vis
    ),
    st.sampled_from(["o1", "o2"]),
    st.sampled_from(list(ScopeLevel)),
    IDS,
    st.sampled_from(list(Visibility)),
)
FILTERS = st.builds(
    ScopeFilter,
    st.sampled_from(["o1", "o2"]),
    st.frozensets(st.builds(Scope, st.sampled_from(list(ScopeLevel)), IDS), max_size=6),
)


@pytest.fixture(scope="module")
def sync_conn():
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(
        "CREATE TEMP TABLE acl_t (i integer, org_id text, scope_level smallint, "
        "scope_owner text, visibility text)"
    )
    yield conn
    conn.close()


@settings(max_examples=100, deadline=None)
@given(FILTERS, st.lists(ACLS, min_size=1, max_size=8))
def test_sql_prefilter_agrees_with_the_python_rule(sync_conn, flt, acls):
    sync_conn.execute("TRUNCATE acl_t")
    for i, a in enumerate(acls):
        sync_conn.execute(
            "INSERT INTO acl_t VALUES (%s,%s,%s,%s,%s)",
            (i, a.org_id, int(a.scope.level), a.scope.owner, str(a.visibility)),
        )
    where, params = acl_where(flt)
    rows = sync_conn.execute(f"SELECT i FROM acl_t WHERE {where}", params).fetchall()  # noqa: S608
    assert {r[0] for r in rows} == {i for i, a in enumerate(acls) if flt.tier(a) is not None}
