"""SQLite-specific behaviour: durability, migrations, locking, SQL access filter, blob paths."""

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hive_mcp.adapters.fs.blobs import FsBlobs
from hive_mcp.adapters.sqlite import SqliteBackend
from hive_mcp.adapters.sqlite.acl import acl_where
from hive_mcp.adapters.sqlite.db import StorageBusy
from hive_mcp.adapters.sqlite.schema import MIGRATIONS
from hive_mcp.adapters.sqlite.text import match_expression
from hive_mcp.domain.access import Acl, ScopeFilter
from hive_mcp.domain.errors import HiveError, NotFound, ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.ports.blobs import BlobRef
from tests.conformance.factory import ALICE, make_item


async def test_data_survives_restart(tmp_path):
    first = SqliteBackend(tmp_path)
    item = make_item(first.clock)
    await first.items.create(ALICE, item, idempotency_key="k")
    first.close()
    second = SqliteBackend(tmp_path)
    assert (await second.items.get(ALICE, item.id)).item == item
    assert not (await second.items.create(ALICE, item, idempotency_key="k")).created
    second.close()


def test_refuses_database_from_the_future(tmp_path):
    SqliteBackend(tmp_path).close()
    conn = sqlite3.connect(tmp_path / "hive.db")
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    conn.close()
    with pytest.raises(HiveError, match="newer"):
        SqliteBackend(tmp_path)


async def test_locked_database_is_a_retryable_error_not_a_crash(tmp_path):
    backend = SqliteBackend(tmp_path, busy_seconds=0.2)
    blocker = sqlite3.connect(tmp_path / "hive.db", isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    with pytest.raises(StorageBusy):
        await backend.items.create(ALICE, make_item(backend.clock))
    blocker.execute("ROLLBACK")
    blocker.close()
    item = make_item(backend.clock)  # the same backend keeps working afterwards
    assert (await backend.items.create(ALICE, item)).created
    backend.close()


async def test_failed_transaction_leaves_nothing_behind(tmp_path):
    backend = SqliteBackend(tmp_path)
    item = make_item(backend.clock, tags=frozenset({"a"}))
    await backend.items.create(ALICE, item)
    clash = make_item(backend.clock, item_id=item.id, tags=frozenset({"b"}))
    with pytest.raises(HiveError):
        await backend.items.create(ALICE, clash)
    assert (await backend.items.get(ALICE, item.id)).item.tags == {"a"}
    backend.close()


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


@settings(max_examples=150, deadline=None)
@given(FILTERS, st.lists(ACLS, min_size=1, max_size=8))
def test_sql_prefilter_agrees_with_the_python_rule(flt, acls):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE t (i INTEGER, org_id TEXT, scope_level INTEGER, "
        "scope_owner TEXT, visibility TEXT)"
    )
    for i, a in enumerate(acls):
        conn.execute(
            "INSERT INTO t VALUES (?,?,?,?,?)",
            (i, a.org_id, int(a.scope.level), a.scope.owner, str(a.visibility)),
        )
    where, params = acl_where(flt)
    sql_visible = {r[0] for r in conn.execute(f"SELECT i FROM t WHERE {where}", params)}  # noqa: S608
    assert sql_visible == {i for i, a in enumerate(acls) if flt.tier(a) is not None}


async def test_blob_paths_cannot_escape_the_root(tmp_path):
    store = FsBlobs(tmp_path / "blobs")
    ref = await store.put("../../evil", b"data")
    stored = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(stored) == 1 and (tmp_path / "blobs").resolve() in stored[0].resolve().parents
    assert await store.get(ref) == b"data"
    for bad in ("../../etc/passwd", "ab", "g" * 64, ""):
        with pytest.raises(ValidationError):
            await store.get(BlobRef("org", bad, 1))


async def test_blob_corruption_is_detected(tmp_path):
    store = FsBlobs(tmp_path / "blobs")
    ref = await store.put("org", b"good")
    path = next(p for p in (tmp_path / "blobs").rglob("*") if p.is_file())
    path.write_bytes(b"tampered")
    with pytest.raises(HiveError, match="corrupted"):
        await store.get(ref)
    await store.delete(ref)
    with pytest.raises(NotFound):
        await store.get(ref)


def test_fts_match_expression_neutralises_operators():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [("near and not",), ("plain words",), ("star",)])
    hostile = ["NEAR", "AND", "NOT", "star*", 'a"b', "col:x", "(", "-x", "^y", "OR"]
    for term in hostile:  # raw terms like these are FTS5 syntax; quoted they are literal text
        conn.execute("SELECT rowid FROM t WHERE t MATCH ?", (match_expression([term]),)).fetchall()
    rows = conn.execute(
        "SELECT x FROM t WHERE t MATCH ?", (match_expression(["NEAR", "plain"]),)
    ).fetchall()
    assert {r[0] for r in rows} == {
        "near and not",
        "plain words",
    }  # quoted NEAR is just the word 'near'
