"""The in-memory matrix answers exactly as the SQL scan does, and follows every writer."""

# ruff: noqa: S311
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import numpy as np
import pytest

from hive_mcp.adapters.sqlite import vector_cache
from hive_mcp.adapters.sqlite.db import Database
from hive_mcp.adapters.sqlite.vectors import SqliteVectors
from hive_mcp.domain.access import Acl, ScopeFilter
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from pathlib import Path

DIM = 16
MODEL = "m1"
ORGS = ["o1", "o2", "o3"]


def unit(rng: random.Random, near: np.ndarray | None = None) -> np.ndarray:
    v = np.array([rng.gauss(0, 1) for _ in range(DIM)], dtype=np.float32)
    if near is not None:
        v = near + 0.05 * v
    return v / np.linalg.norm(v)


def scopes() -> list[Scope]:
    return (
        [Scope(ScopeLevel.PRIVATE, f"u{n}") for n in range(6)]
        + [Scope(ScopeLevel.PROJECT, f"p{n}") for n in range(4)]
        + [Scope(ScopeLevel.TEAM, f"t{n}") for n in range(3)]
    )


async def populate(vectors: SqliteVectors, rng: random.Random, items: int = 300) -> dict:
    """Items over three organisations, many scopes, org-wide and not, one to many chunks each
    (including exact duplicate chunks, which tie)."""
    meta = {}
    pool = scopes()
    for n in range(items):
        org = rng.choice(ORGS)
        scope = rng.choice(pool)
        visibility = (
            Visibility.ORG
            if rng.random() < 0.3 and scope.level is not ScopeLevel.PRIVATE
            else Visibility.SCOPE
        )
        chunks = [unit(rng) for _ in range(rng.choice([1, 1, 2, 3, 6, 25]))]
        if rng.random() < 0.1:
            chunks.append(chunks[0].copy())  # an exact tie inside one item
        item_id = f"item{n:04d}"
        await vectors.replace(item_id, MODEL, Acl(org, scope, visibility), chunks)
        meta[item_id] = (org, scope, visibility)
    return meta


def principal(rng: random.Random) -> ScopeFilter:
    member = frozenset(rng.sample(scopes(), rng.randint(0, 6)))
    return ScopeFilter(rng.choice(ORGS), member)


def same(a, b) -> None:
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert x.score == pytest.approx(y.score, abs=2e-6)
        if x.item_id != y.item_id:  # only a tie to within float noise may order differently
            assert abs(x.score - y.score) < 2e-6


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "v.db")
    yield database
    database.close()


@pytest.fixture
def pair(db):
    return SqliteVectors(db, cache_mb=64), SqliteVectors(db, cache_mb=0)


async def compare(cached, plain, rng, searches: int = 40) -> None:
    for _ in range(searches):
        flt, q = principal(rng), unit(rng)
        for limit in (1, 3, 10, 60):
            same(
                await cached.search(flt, MODEL, q, limit), await plain.search(flt, MODEL, q, limit)
            )


async def test_answers_match_the_scan_for_many_principals_and_limits(pair):
    cached, plain = pair
    rng = random.Random(1)
    await populate(cached, rng)
    await compare(cached, plain, rng)


async def test_one_item_with_many_chunks_cannot_crowd_out_the_others(pair):
    cached, plain = pair
    rng = random.Random(2)
    target = unit(rng)
    acl = Acl("o1", Scope(ScopeLevel.PROJECT, "p1"), Visibility.SCOPE)
    await cached.replace("dominant", MODEL, acl, [unit(rng, target) for _ in range(300)])
    for n in range(12):
        await cached.replace(f"other{n:02d}", MODEL, acl, [unit(rng, target)])
    flt = ScopeFilter("o1", frozenset({Scope(ScopeLevel.PROJECT, "p1")}))
    got = await cached.search(flt, MODEL, target, 5)
    assert len({h.item_id for h in got}) == 5
    same(got, await plain.search(flt, MODEL, target, 5))


async def test_a_second_connection_writing_is_seen_by_the_next_search(db):
    other_db = Database(db._conn.execute("PRAGMA database_list").fetchone()[2])
    a, writer = SqliteVectors(db, cache_mb=64), SqliteVectors(other_db, cache_mb=0)
    rng = random.Random(3)
    acl = Acl("o1", Scope(ScopeLevel.PROJECT, "p1"), Visibility.SCOPE)
    flt = ScopeFilter("o1", frozenset({Scope(ScopeLevel.PROJECT, "p1")}))
    q = unit(rng)
    await writer.replace("x", MODEL, acl, [q])
    assert [h.item_id for h in await a.search(flt, MODEL, q, 3)] == ["x"]  # loaded now
    await writer.replace("y", MODEL, acl, [q * 0.9 + 0.1 * unit(rng)])  # a new item elsewhere
    assert {h.item_id for h in await a.search(flt, MODEL, q, 3)} == {"x", "y"}
    await writer.replace("x", MODEL, acl, [-q])  # its vector changes
    assert (await a.search(flt, MODEL, q, 1))[0].item_id == "y"
    await writer.update_acl("y", Acl("o1", Scope(ScopeLevel.PROJECT, "p9"), Visibility.SCOPE))
    assert [h.item_id for h in await a.search(flt, MODEL, q, 3)] == ["x"]  # moved out of view
    await writer.update_acl("y", Acl("o1", Scope(ScopeLevel.PROJECT, "p9"), Visibility.ORG))
    assert {h.item_id for h in await a.search(flt, MODEL, q, 3)} == {"x", "y"}  # org-wide
    await writer.delete("x")
    assert [h.item_id for h in await a.search(flt, MODEL, q, 3)] == ["y"]
    other_db.close()


async def test_changes_are_patched_in_not_reloaded(db, monkeypatch):
    cached = SqliteVectors(db, cache_mb=64)
    rng = random.Random(4)
    await populate(cached, rng, 80)
    loads = []
    real = vector_cache.ModelCache._load
    monkeypatch.setattr(
        vector_cache.ModelCache, "_load", lambda self, c, n: (loads.append(1), real(self, c, n))[1]
    )
    flt = ScopeFilter("o1", frozenset(scopes()))
    q = unit(rng)
    await cached.search(flt, MODEL, q, 5)
    for n in range(20):
        await cached.replace(
            f"item{n:04d}", MODEL, Acl("o1", scopes()[0], Visibility.SCOPE), [unit(rng)]
        )
        await cached.search(flt, MODEL, q, 5)
    assert loads == [1]  # one load for the first search; the twenty changes were patched in


async def test_a_big_change_or_a_trimmed_log_reloads_and_stays_correct(pair, monkeypatch):
    cached, plain = pair
    rng = random.Random(5)
    await populate(cached, rng, 120)
    await compare(cached, plain, rng, 3)
    monkeypatch.setattr(vector_cache, "RELOAD_MIN", 3)
    monkeypatch.setattr(vector_cache, "RELOAD_SHARE", 0.01)
    monkeypatch.setattr(vector_cache, "KEEP_LOG", 5)  # trimmed hard: a sleeping reader falls behind
    acl = Acl("o1", scopes()[7], Visibility.ORG)
    for n in range(40):
        await plain.replace(f"item{n:04d}", MODEL, acl, [unit(rng), unit(rng)])
    await compare(cached, plain, rng, 10)


async def test_dead_rows_are_squeezed_out_without_changing_answers(pair, monkeypatch):
    from hive_mcp.adapters.sqlite import vector_matrix

    cached, plain = pair
    monkeypatch.setattr(vector_matrix, "COMPACT_MIN", 5)
    rng = random.Random(6)
    await populate(cached, rng, 60)
    flt = ScopeFilter("o1", frozenset(scopes()))
    await cached.search(flt, MODEL, unit(rng), 5)  # loaded
    for _ in range(4):
        for n in range(60):
            await plain.replace(
                f"item{n:04d}",
                MODEL,
                Acl("o1", scopes()[6 + n % 4], Visibility.ORG),
                [unit(rng)] * 3,
            )
        await cached.search(flt, MODEL, unit(rng), 5)
    matrix = cached._cache._models[MODEL].matrix
    assert matrix.dead < 0.3 * matrix.n  # compaction ran
    await compare(cached, plain, rng, 15)


async def test_a_model_over_the_memory_cap_is_left_to_the_scan_with_one_warning(db, caplog):
    cached, plain = SqliteVectors(db, cache_mb=1), SqliteVectors(db, cache_mb=0)
    rng = random.Random(7)
    big = 384
    acl = Acl("o1", scopes()[1], Visibility.SCOPE)
    for n in range(800):  # 800 chunks of 384 floats: about 1.2 MB, over the 1 MiB cap
        v = np.array([rng.gauss(0, 1) for _ in range(big)], dtype=np.float32)
        await cached.replace(f"i{n:04d}", "big", acl, [v / np.linalg.norm(v)])
    flt = ScopeFilter("o1", frozenset({scopes()[1]}))
    q = np.ones(big, dtype=np.float32) / np.sqrt(big)
    with caplog.at_level(logging.WARNING, logger="hive.vectors"):
        first = await cached.search(flt, "big", q, 5)
        second = await cached.search(flt, "big", q, 5)
    same(first, await plain.search(flt, "big", q, 5))
    assert first == second and len([r for r in caplog.records if "does not fit" in r.message]) == 1


async def test_dropping_a_model_and_wrong_dimensions(pair):
    cached, _ = pair
    rng = random.Random(8)
    acl = Acl("o1", scopes()[1], Visibility.SCOPE)
    flt = ScopeFilter("o1", frozenset({scopes()[1]}))
    q = unit(rng)
    await cached.replace("a", MODEL, acl, [q])
    assert await cached.search(flt, MODEL, q, 3)
    with pytest.raises(ValidationError, match="dimension"):
        await cached.search(flt, MODEL, np.ones(DIM + 1, dtype=np.float32), 3)
    await cached.drop_model(MODEL)
    assert await cached.search(flt, MODEL, q, 3) == []


async def test_nothing_visible_nothing_found(pair):
    cached, _ = pair
    rng = random.Random(9)
    await cached.replace("a", MODEL, Acl("o1", scopes()[1], Visibility.SCOPE), [unit(rng)])
    assert await cached.search(ScopeFilter("o1", frozenset()), MODEL, unit(rng), 3) == []
    assert (
        await cached.search(ScopeFilter("o2", frozenset({scopes()[1]})), MODEL, unit(rng), 3) == []
    )


async def test_searches_and_writes_at_the_same_time_stay_consistent(pair):
    cached, plain = pair
    rng = random.Random(10)
    await populate(cached, rng, 100)
    flt = ScopeFilter("o1", frozenset(scopes()))
    errors: list[BaseException] = []

    async def reader() -> None:
        r = random.Random(11)
        for _ in range(60):
            try:
                hits = await cached.search(flt, MODEL, unit(r), 8)
                assert len({h.item_id for h in hits}) == len(hits)
            except BaseException as exc:
                errors.append(exc)

    async def writer() -> None:
        r = random.Random(12)
        for n in range(60):
            await plain.replace(
                f"item{n:04d}", MODEL, Acl("o1", scopes()[6 + n % 4], Visibility.ORG), [unit(r)]
            )
            await asyncio.sleep(0)

    await asyncio.gather(reader(), reader(), writer())
    assert not errors, errors[:1]
    await compare(cached, plain, rng, 10)
