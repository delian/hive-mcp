"""The shared vector-index conformance tests, run against the LanceDB store (plus its own cases)."""

from __future__ import annotations

import numpy as np
import pytest
import pytest_asyncio

pytest.importorskip("lancedb")

from hive_mcp.adapters.lance.vectors import LanceVectors, acl_filter, quote, table_name
from hive_mcp.adapters.sqlite import SqliteBackend
from hive_mcp.domain.access import Acl, ScopeFilter, read_filter
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from tests.conformance.factory import ALICE, BOB, P1
from tests.conformance.test_vectors import *  # noqa: F403 - the same tests, a different store

ACL1 = Acl("org1", P1, Visibility.SCOPE)


def v(*xs):
    return np.array(xs, dtype=np.float32)


@pytest_asyncio.fixture
async def backend(
    tmp_path,
):  # overrides the parametrised fixture: SQLite for everything but vectors
    sqlite = SqliteBackend(tmp_path / "data", vectors=LanceVectors(tmp_path / "lance"))
    yield sqlite
    sqlite.close()


async def test_hostile_scope_names_cannot_break_out_of_the_filter(backend):
    evil = Scope(ScopeLevel.PROJECT, "x') OR visibility = 'org' OR ('1'='1")
    await backend.vectors.replace(
        "secret", "m", Acl("org1", Scope(ScopeLevel.PROJECT, "other"), Visibility.SCOPE), [v(1, 0)]
    )
    flt = ScopeFilter("org1", frozenset({evil}))
    assert await backend.vectors.search(flt, "m", v(1, 0), 10) == []
    await backend.vectors.replace("mine", "m", Acl("org1", evil, Visibility.SCOPE), [v(1, 0)])
    assert [h.item_id for h in await backend.vectors.search(flt, "m", v(1, 0), 10)] == ["mine"]
    other_org = ScopeFilter("org1' OR '1'='1", frozenset({evil}))
    assert await backend.vectors.search(other_org, "m", v(1, 0), 10) == []


def test_quoting_and_filter_text():
    assert quote("a'b") == "'a''b'" and quote("") == "''"
    with pytest.raises(ValidationError):
        quote("a\nb")
    assert acl_filter(ScopeFilter("o", frozenset())) == "org_id = 'o' AND (visibility = 'org')"
    assert "(scope_level = 1 AND scope_owner = 'p1')" in acl_filter(read_filter(ALICE))


async def test_many_chunks_of_one_item_do_not_hide_other_items(backend):
    await backend.vectors.replace("hog", "m", ACL1, [v(1, 0.001 * i) for i in range(200)])
    await backend.vectors.replace("other", "m", ACL1, [v(0.6, 0.8)])
    hits = await backend.vectors.search(read_filter(BOB), "m", v(1, 0), 2)
    assert [h.item_id for h in hits] == ["hog", "other"]  # the search looked past the hog's chunks


async def test_unicode_ids_and_owners_round_trip(backend):
    scope = Scope(ScopeLevel.PROJECT, "проект-ß's")
    await backend.vectors.replace("ключ-1", "m", Acl("org1", scope, Visibility.SCOPE), [v(0, 1)])
    flt = ScopeFilter("org1", frozenset({scope}))
    assert [h.item_id for h in await backend.vectors.search(flt, "m", v(0, 1), 5)] == ["ключ-1"]
    await backend.vectors.delete("ключ-1")
    assert await backend.vectors.search(flt, "m", v(0, 1), 5) == []


async def test_vectors_survive_reopening_the_store(tmp_path):
    first = LanceVectors(tmp_path / "lance")
    await first.replace("a", "model/x|1", ACL1, [v(1, 0, 0)])
    again = LanceVectors(tmp_path / "lance")
    assert await again.model_stats() == {"model/x|1": 1}
    assert [
        h.item_id for h in await again.search(read_filter(ALICE), "model/x|1", v(1, 0, 0), 3)
    ] == ["a"]


async def test_an_emptied_model_is_still_listed_with_zero_items(backend):
    await backend.vectors.replace("a", "m", ACL1, [v(1, 0)])
    await backend.vectors.replace("a", "m", ACL1, [])
    assert await backend.vectors.model_stats() == {"m": 0}
    assert await backend.vectors.drop_model("m") == 0 and await backend.vectors.model_stats() == {}


def test_table_names_are_stable_and_distinct():
    assert table_name("a") == table_name("a") != table_name("b")
    assert table_name("weird/model|name").replace("_", "").isalnum()


async def test_concurrent_replaces_do_not_lose_or_duplicate_chunks(backend):
    import asyncio  # noqa: PLC0415

    await asyncio.gather(
        *(
            backend.vectors.replace(f"i{n}", "m", ACL1, [v(1, n / 10), v(n / 10, 1)])
            for n in range(12)
        )
    )
    await asyncio.gather(
        *(backend.vectors.replace(f"i{n}", "m", ACL1, [v(1, n / 10)]) for n in range(12))
    )
    assert (await backend.vectors.model_stats()) == {"m": 12}
    hits = await backend.vectors.search(read_filter(ALICE), "m", v(1, 0), 50)
    assert sorted(h.item_id for h in hits) == sorted(f"i{n}" for n in range(12)) and {
        h.chunk_ord for h in hits
    } == {0}


async def test_the_access_filter_applies_before_the_nearest_neighbours_are_picked(backend):
    hidden = Acl("org1", Scope(ScopeLevel.PROJECT, "elsewhere"), Visibility.SCOPE)
    for n in range(
        300
    ):  # many invisible vectors that are closer to the query than anything visible
        await backend.vectors.replace(f"h{n}", "m", hidden, [v(1, 0.0001 * n)])
    for n in range(3):
        await backend.vectors.replace(f"v{n}", "m", ACL1, [v(0.05 * (n + 1), 1)])
    hits = await backend.vectors.search(read_filter(ALICE), "m", v(1, 0), 3)
    assert sorted(h.item_id for h in hits) == ["v0", "v1", "v2"]
