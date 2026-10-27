"""Access control under load: a mixed population is searched and listed by everybody; every result
must be permitted by an oracle computed from plain membership tables, and nothing permitted that
should be found may be missing. Then the same with thousands of notes: latency and paging."""

from __future__ import annotations

import math
import time

import pytest

from hive_mcp.domain.item import Cursor, ItemQuery, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, ForeignPolicy
from tests.stress.population import PEOPLE, build, oracle, principal
from tests.stress.support import SCALE, gather_bounded, percentile, rng

pytestmark = pytest.mark.stress

ROWS = {r[0]: r for r in PEOPLE}
ALL_STATUSES = frozenset(Status)


async def listing(stack, who, statuses=None) -> list:
    """The whole browse listing by cursor, newest first."""
    out, after, calls = [], None, 0
    while True:
        page = await stack.knowledge.browse(
            who,
            ItemQuery(
                limit=100,
                after=after,
                statuses=statuses or frozenset({Status.ACTIVE, Status.DRAFT}),
            ),
        )
        calls += 1
        assert len(page.items) <= 100
        out += [v for v in page.items]
        if page.next_cursor is None:
            return out
        after = Cursor.decode(page.next_cursor)


def assert_listing_exact(pop, row, views) -> None:
    ids = [v.item.id for v in views]
    assert len(ids) == len(set(ids)), "an item was listed twice"
    assert set(ids) == pop.listing(row), "listing differs from the oracle"
    keys = [(v.item.created_at, v.item.id) for v in views]
    assert keys == sorted(keys, reverse=True), "not newest first with a stable tie-break"
    for v in views:
        assert v.tier.value == oracle(row, pop.notes[v.item.id])


async def test_every_search_result_is_permitted_and_every_permitted_note_is_found(stack) -> None:
    pop = await build(stack, 500 * SCALE, seed=1)
    gen = rng(2)
    probes = gen.sample(sorted(pop.notes), 60)

    async def ask(person: str, note_id: str):
        who = principal(ROWS[person])
        note = pop.notes[note_id]
        res = await stack.consult.consult(
            who, ConsultRequest(note.token, limit=50, include_foreign=ForeignPolicy.ALWAYS)
        )
        return person, note, res

    jobs = [(lambda p=p, n=n: ask(p, n)) for p in ROWS for n in probes]
    outcomes = await gather_bounded(jobs, 24)
    assert not [o for o in outcomes if isinstance(o, BaseException)]
    exact_hits = 0
    for person, note, res in outcomes:
        row = ROWS[person]
        got = {m.item_id: m.tier.value for m in (*res.primary, *res.referenced)}
        assert {m.item_id for m in res.primary} == {i for i, t in got.items() if t == "primary"}
        for item_id, tier in got.items():  # nothing forbidden, nothing mislabelled
            assert oracle(row, pop.notes[item_id]) == tier, (person, item_id)
            assert pop.notes[item_id].status in {Status.ACTIVE, Status.DRAFT}
        expected = oracle(row, note)
        if expected and note.status is not Status.ARCHIVED:
            assert got.get(note.id) == expected, f"{person} cannot find permitted {note.id}"
            exact_hits += 1
        else:
            assert note.id not in got, f"{person} found forbidden {note.id}"
    assert exact_hits > 100  # the probe set really had permitted notes


async def test_listings_match_the_oracle_for_everyone_with_many_equal_timestamps(stack) -> None:
    pop = await build(stack, 500 * SCALE, seed=3)
    for row in PEOPLE:
        views = await listing(stack, principal(row))
        assert_listing_exact(pop, row, views)
    # the archived ones too, when asked for explicitly
    for row in PEOPLE[:3]:
        views = await listing(stack, principal(row), ALL_STATUSES)
        assert {v.item.id for v in views} == pop.listing(row, ALL_STATUSES)


async def test_permissions_hold_while_notes_are_moved_and_archived_under_the_searchers(
    stack,
) -> None:
    pop = await build(stack, 400 * SCALE, seed=4)
    gen = rng(5)
    owners = {r[0]: principal(r) for r in PEOPLE}
    # private notes of acme members move to project p1 (self-service) while everyone searches
    movers = [
        n for n in pop.notes.values()
        if n.org == "acme" and n.level == "private" and n.status is Status.ACTIVE
        and ROWS[n.owner][2] >= 1 and "p1" in ROWS[n.owner][3]
    ]  # fmt: skip
    movers = movers[:40]
    assert movers, "population has no movable notes"
    target = Scope(ScopeLevel.PROJECT, "p1")
    old = {n.id: (n.level, n.owner) for n in movers}

    async def move(note):
        who = owners[note.owner]
        current = (await stack.knowledge.get(who, note.id)).item
        await stack.knowledge.move(who, note.id, target, Visibility.SCOPE, current.revision)

    def allowed(row, note, moved: bool | None) -> bool:
        """Permitted before the move, after it, or (moved is None) either; always by the oracle."""
        before = oracle(row, note) is not None
        saved = (note.level, note.owner)
        note.level, note.owner = "project", "p1"
        after = oracle(row, note) is not None
        note.level, note.owner = saved
        return before or after if moved is None else (after if moved else before)

    probes = [n.token for n in gen.sample(movers, 15)] + ["pelican shared words"]
    searches = [
        (lambda p=p, q=q: stack.consult.consult(
            principal(ROWS[p]), ConsultRequest(q, limit=60, include_foreign=ForeignPolicy.ALWAYS)
        ))
        for p in ROWS for q in probes
    ]  # fmt: skip
    outcomes = await gather_bounded([(lambda n=n: move(n)) for n in movers] + searches, 16)
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes[:3]
    # during the storm any result must be permitted before or after its move
    idx = len(movers)
    for p in ROWS:  # noqa: PLC0206 - the order of ROWS is the order the searches were queued
        for _q in probes:
            res = outcomes[idx]
            idx += 1
            for m in (*res.primary, *res.referenced):
                note = pop.notes[m.item_id]
                assert allowed(ROWS[p], note, None if note.id in old else False), (p, note.id)
    for n in movers:  # settle: the oracle now sees the new place
        n.level, n.owner = "project", "p1"
    for row in PEOPLE:
        res = await stack.consult.consult(
            principal(row),
            ConsultRequest("pelican shared words", limit=100, include_foreign=ForeignPolicy.ALWAYS),
        )
        for m in (*res.primary, *res.referenced):
            assert oracle(row, pop.notes[m.item_id]) == m.tier.value
        assert_listing_exact(pop, row, await listing(stack, principal(row)))


async def test_search_latency_and_whole_listing_paging_with_thousands_of_notes(stack) -> None:
    # 5000 notes (2500 on Postgres, where each write is a round trip) times SCALE, at most 20000
    base = 2500 if type(stack.backend).__name__ == "PostgresBackend" else 5000
    count = min(base * SCALE, 20_000)
    t0 = time.perf_counter()
    pop = await build(stack, count, seed=6)
    built = time.perf_counter() - t0
    gen = rng(7)
    people = [ROWS[k] for k in ("m1", "m2", "m3", "m4", "e1", "adm", "g1")]
    latencies = []
    for _ in range(80):
        row, note = gen.choice(people), pop.notes[f"n{gen.randrange(count):06d}"]
        who = principal(row)
        query = f"{note.token} group{gen.randrange(40)}" if gen.random() < 0.5 else "pelican shared"
        t = time.perf_counter()
        res = await stack.consult.consult(
            who, ConsultRequest(query, limit=50, include_foreign=ForeignPolicy.ALWAYS)
        )
        latencies.append(time.perf_counter() - t)
        for m in (*res.primary, *res.referenced):
            assert oracle(row, pop.notes[m.item_id]) == m.tier.value
        want = oracle(row, note)
        if f"{note.token}" in query and want and note.status is not Status.ARCHIVED:
            assert note.id in {m.item_id for m in (*res.primary, *res.referenced)}
    p95 = percentile(latencies, 95)
    print(f"\n[stress] {count} notes built in {built:.1f}s; search p50 "
          f"{percentile(latencies, 50) * 1000:.0f} ms p95 {p95 * 1000:.0f} ms")  # fmt: skip
    assert p95 < 1.0, f"search p95 {p95:.3f}s with {count} notes"

    for row in (ROWS["m2"], ROWS["m4"], ROWS["g1"]):  # paging the whole listing, page by page
        who, seen, after, previous, pages = principal(row), set(), None, None, 0
        while True:
            page = await stack.knowledge.browse(who, ItemQuery(limit=100, after=after))
            pages += 1
            assert len(page.items) <= 100
            for v in page.items:
                key = (v.item.created_at, v.item.id)
                assert previous is None or key < previous, "order broken or an item repeated"
                previous = key
                assert v.item.id not in seen
                seen.add(v.item.id)
            if page.next_cursor is None:
                break
            after = Cursor.decode(page.next_cursor)
        assert seen == pop.listing(row)
        assert pages == math.ceil(len(seen) / 100) or pages == len(seen) // 100 + 1
