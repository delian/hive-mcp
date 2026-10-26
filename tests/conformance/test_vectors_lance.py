"""The shared vector-index conformance tests, run against the LanceDB store (plus its own cases)."""

from __future__ import annotations

import asyncio

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


# ---------------------------------------------------------------- the approximate index
def wait_for(predicate, seconds=30.0):
    import time  # noqa: PLC0415

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


async def fill(store, n, start=0, dim=16, seed=0):
    rng = np.random.default_rng(seed + start)
    centres = np.random.default_rng(99).standard_normal((12, dim))
    vecs = []
    for i in range(start, start + n):
        v = (centres[i % 12] + 0.3 * rng.standard_normal(dim)).astype(np.float32)
        vecs.append(v)
        acl = Acl("org1", Scope(ScopeLevel.PROJECT, f"g{i % 6}"), Visibility.SCOPE)
        await store.replace(f"i{i}", "m", acl, [v])
    return np.array(vecs)


async def test_a_big_enough_table_gets_an_index_in_the_background_and_still_answers_correctly(
    tmp_path, monkeypatch
):
    from hive_mcp.adapters.lance import vectors as module  # noqa: PLC0415

    monkeypatch.setattr(module, "OPTIMIZE_EVERY", 50)
    store = LanceVectors(tmp_path / "lance", index_min_rows=200)
    vecs = await fill(store, 300)
    table = store._open("m")
    assert wait_for(lambda: store._ann.has_index(table)), "the index was never built"
    flt = ScopeFilter("org1", frozenset({Scope(ScopeLevel.PROJECT, f"g{g}") for g in (0, 1, 2)}))
    allowed = np.array([i for i in range(300) if i % 6 in (0, 1, 2)])
    rng = np.random.default_rng(5)
    found = total = 0
    for _ in range(20):
        q = rng.standard_normal(16).astype(np.float32)
        exact = allowed[
            np.argsort(-(vecs[allowed] @ q) / np.linalg.norm(vecs[allowed], axis=1))[:5]
        ]
        hits = await store.search(flt, "m", q, 5)
        assert all(
            int(h.item_id[1:]) % 6 in (0, 1, 2) for h in hits
        )  # the filter holds with an index
        found += len({int(h.item_id[1:]) for h in hits} & set(exact.tolist()))
        total += 5
    assert found / total >= 0.9


async def test_small_tables_stay_exact_and_get_no_index(tmp_path, monkeypatch):
    from hive_mcp.adapters.lance import vectors as module  # noqa: PLC0415

    monkeypatch.setattr(module, "OPTIMIZE_EVERY", 20)
    store = LanceVectors(tmp_path / "lance", index_min_rows=10_000)
    await fill(store, 60)
    assert (
        not store._ann.has_index(store._open("m")) and store._ann.probes(store._open("m")) is None
    )


async def test_probes_cover_most_partitions_and_can_be_overridden(tmp_path, monkeypatch):
    store = LanceVectors(tmp_path / "lance")
    await fill(store, 5)
    table = store._open("m")
    monkeypatch.setattr(store._ann, "has_index", lambda t: True)
    for built, probes in (
        (220_000, 399),
        (100, 9),
        (36, 7),
    ):  # ceil(0.85 * sqrt(n)), never below 8 partitions' worth
        store._ann._remember_build(table.name, built)
        store._ann._info.clear()
        assert store._ann.probes(table) == probes, built
    store._ann.nprobes = 3
    assert store._ann.probes(table) == 3


async def test_the_build_size_survives_a_restart(tmp_path):
    first = LanceVectors(tmp_path / "lance")
    first._ann._remember_build("vec_x", 123_456)
    assert LanceVectors(tmp_path / "lance")._ann._built_rows("vec_x") == 123_456
    assert LanceVectors(tmp_path / "lance")._ann._built_rows("vec_other") == 0


async def test_the_index_is_rebuilt_only_after_the_table_doubles(tmp_path, monkeypatch):
    from hive_mcp.adapters.lance import vectors as module  # noqa: PLC0415

    monkeypatch.setattr(module, "OPTIMIZE_EVERY", 50)
    store = LanceVectors(tmp_path / "lance", index_min_rows=100)
    builds = []
    real = store._ann._build_index

    def counting(name, rows):
        builds.append(rows)
        real(name, rows)

    monkeypatch.setattr(store._ann, "_build_index", counting)
    await fill(store, 150)  # the compaction at the 100th write finds 100 rows: first build
    assert wait_for(lambda: builds == [100] and not store._ann.building)
    await fill(store, 49, start=150)  # 199 rows: the compaction at write 150 saw 150, below double
    await asyncio.sleep(0.5)
    assert builds == [100]
    await fill(store, 1, start=199)  # the 200th write: double the size at the last build
    assert wait_for(lambda: builds == [100, 200] and not store._ann.building)


async def test_a_failing_index_build_leaves_searches_working_and_is_retried(tmp_path, monkeypatch):
    from hive_mcp.adapters.lance import vectors as module  # noqa: PLC0415

    monkeypatch.setattr(module, "OPTIMIZE_EVERY", 50)
    store = LanceVectors(tmp_path / "lance", index_min_rows=100)
    calls = []

    def broken(name, rows):
        calls.append(name)
        store._ann.building.discard(name)

    monkeypatch.setattr(store._ann, "_build_index", broken)
    vecs = await fill(store, 160)
    assert wait_for(lambda: len(calls) >= 1)
    q = vecs[7]
    flt = ScopeFilter("org1", frozenset({Scope(ScopeLevel.PROJECT, f"g{g}") for g in range(6)}))
    assert (await store.search(flt, "m", q, 1))[0].item_id == "i7"
