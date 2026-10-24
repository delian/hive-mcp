"""Edge cases and concurrency every backend must survive."""

import asyncio
from dataclasses import replace
from datetime import UTC, timedelta, timezone

import pytest

from hive_mcp.domain.access import Acl, read_filter
from hive_mcp.domain.errors import Conflict
from hive_mcp.domain.item import Cursor, ItemPatch, ItemQuery
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.ports.indexes import TextDoc
from tests.conformance.factory import ALICE, EDDIE, P1, make_item

KINDS = frozenset({"ingest"})


async def test_items_order_by_true_instant_across_time_zones(backend):
    base = backend.clock.now()
    # Adversarial: as *text* the local readings sort reversed (07 < 11 < 12), but the true
    # instants are tz0 = 12:00Z (newest), tz1 = 11:00Z, tz2 = 10:00Z.
    written = {
        "tz0": base.replace(hour=12).astimezone(timezone(timedelta(hours=-5))),  # 07:00-05:00
        "tz1": base.replace(hour=11).astimezone(UTC),  # 11:00+00:00
        "tz2": base.replace(hour=10).astimezone(timezone(timedelta(hours=2))),  # 12:00+02:00
    }
    for item_id, when in written.items():
        item = replace(make_item(backend.clock, item_id=item_id), created_at=when, updated_at=when)
        await backend.items.create(ALICE, item)
    expected = ["tz0", "tz1", "tz2"]  # newest instant first
    page = await backend.items.list(ALICE, ItemQuery(limit=10))
    assert [v.item.id for v in page.items] == expected
    seen, cursor = [], None
    while True:
        query = ItemQuery(limit=1, after=Cursor.decode(cursor) if cursor else None)
        page = await backend.items.list(ALICE, query)
        seen += [v.item.id for v in page.items]
        cursor = page.next_cursor
        if not cursor:
            break
    assert seen == expected


async def test_principal_with_hundreds_of_scopes(backend):
    big = Principal("big", "org1", Role.MEMBER, projects=frozenset(f"proj{i}" for i in range(800)))
    mine = make_item(backend.clock, author=big, scope=Scope(ScopeLevel.PROJECT, "proj799"))
    await backend.items.create(big, mine)
    other = make_item(backend.clock, author=ALICE, scope=Scope(ScopeLevel.PROJECT, "p1"))
    await backend.items.create(ALICE, other)
    assert [v.item.id for v in (await backend.items.list(big, ItemQuery())).items] == [mine.id]
    await backend.text.upsert(TextDoc(mine.id, mine.acl, "alpha"))
    assert [h.item_id for h in await backend.text.search(read_filter(big), "alpha", 5)] == [mine.id]


@pytest.mark.parametrize(
    "hostile",
    [
        "NEAR(a b)",
        "a AND NOT b",
        '"unbalanced',
        "a*",
        "col:thing",
        "(((",
        "x' OR 1=1 --",
        'a" OR "b',
        "-a",
        "^a",
    ],
)
async def test_hostile_text_queries_never_error(backend, hostile):
    await backend.text.upsert(TextDoc("d", Acl("org1", P1, Visibility.SCOPE), "a b thing x"))
    hits = await backend.text.search(read_filter(ALICE), hostile, 5)
    assert all(h.item_id == "d" for h in hits)  # may or may not match, must never raise or leak


async def test_concurrent_updates_with_same_revision_exactly_one_wins(backend):
    item = make_item(backend.clock)
    await backend.items.create(ALICE, item)
    patches = [ItemPatch(title=f"T{i}") for i in range(8)]
    results = await asyncio.gather(
        *(backend.items.update(ALICE, item.id, p, 1) for p in patches), return_exceptions=True
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    assert len(wins) == 1 and all(
        isinstance(r, Conflict) for r in results if isinstance(r, Exception)
    )
    assert (await backend.items.get(ALICE, item.id)).item.revision == 2


async def test_concurrent_creates_with_one_key_create_once(backend):
    item = make_item(backend.clock)
    results = await asyncio.gather(
        *(backend.items.create(ALICE, item, idempotency_key="same") for _ in range(8))
    )
    assert sum(r.created for r in results) == 1
    assert len((await backend.items.list(ALICE, ItemQuery())).items) == 1


async def test_concurrent_claims_hand_out_each_job_once(backend):
    for _ in range(5):
        await backend.jobs.enqueue("org1", "ingest", {})
    claims = await asyncio.gather(*(backend.jobs.claim(f"w{i}", KINDS, 30) for i in range(12)))
    got = [c.id for c in claims if c]
    assert len(got) == 5 and len(set(got)) == 5


async def test_concurrent_tag_additions_are_all_kept(backend):
    item = make_item(backend.clock)
    await backend.items.create(ALICE, item)
    await backend.items.update(ALICE, item.id, ItemPatch(add_tags=frozenset({"t0"})), 1)

    # Retry loop like a real client: re-read the revision after a Conflict.
    async def add(tag, who):
        for _ in range(20):
            rev = (await backend.items.get(who, item.id)).item.revision
            try:
                await backend.items.update(who, item.id, ItemPatch(add_tags=frozenset({tag})), rev)
                return
            except Conflict:
                await asyncio.sleep(0)
        raise AssertionError("starved")

    await asyncio.gather(*(add(f"x{i}", ALICE if i % 2 else EDDIE) for i in range(6)))
    tags = (await backend.items.get(ALICE, item.id)).item.tags
    assert tags == {"t0", *(f"x{i}" for i in range(6))}
