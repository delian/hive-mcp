from dataclasses import replace
from datetime import timedelta

import pytest

from hive_mcp.domain.edge import Edge
from hive_mcp.domain.item import ItemPatch, Kind, Status
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.ports.maintenance import ItemFilter
from tests.conformance.factory import ALICE, ALICE_PRIVATE, BOB, CAROL, P1, P2, make_item


async def seed(backend):
    c = backend.clock
    items = {}
    items["a_priv"] = make_item(c, ALICE, ALICE_PRIVATE, item_id="i1", kind=Kind.SESSION)
    c.advance(seconds=100)
    items["a_proj"] = make_item(c, ALICE, P1, item_id="i2", kind=Kind.NOTE, tags=frozenset({"x"}))
    items["b_proj"] = make_item(c, BOB, P1, item_id="i3", kind=Kind.SESSION)
    items["c_other"] = make_item(c, CAROL, P2, item_id="i4")
    for who, item in zip((ALICE, ALICE, BOB, CAROL), items.values(), strict=True):
        await backend.items.create(who, item)
    return items


async def test_purge_removes_the_item_its_tags_and_idempotency_keys(backend):
    items = await seed(backend)
    first = await backend.items.create(
        ALICE, make_item(backend.clock, ALICE, P1, item_id="k1"), "key-1"
    )
    assert first.created
    gone = await backend.maintenance.purge_system("org1", "i2")
    assert gone == items["a_proj"]
    assert await backend.items.get_system("org1", "i2") is None
    assert await backend.maintenance.purge_system("org1", "i2") is None  # second time: nothing
    assert (
        await backend.maintenance.purge_system("org2", "i1") is None
    )  # other tenant cannot reach it
    assert await backend.items.get_system("org1", "i1") is not None
    await backend.maintenance.purge_system("org1", "k1")
    again = await backend.items.create(
        ALICE, make_item(backend.clock, ALICE, P1, item_id="k2"), "key-1"
    )
    assert again.created and again.item.id == "k2"  # the key no longer points at a purged item
    assert await backend.items.tag_counts(ALICE) == []  # tags went with the item


async def test_find_system_filters_orders_and_pages(backend):
    await seed(backend)
    ids = lambda rows: [i.id for i in rows]  # noqa: E731
    assert ids(await backend.maintenance.find_system(ItemFilter())) == ["i1", "i2", "i3", "i4"]
    assert ids(
        await backend.maintenance.find_system(ItemFilter(org_id="org1", author_id="alice"))
    ) == ["i1", "i2"]
    assert ids(
        await backend.maintenance.find_system(ItemFilter(kinds=frozenset({Kind.SESSION})))
    ) == ["i1", "i3"]
    assert ids(await backend.maintenance.find_system(ItemFilter(private_only=True))) == ["i1"]
    cutoff = backend.clock.now() - timedelta(seconds=50)
    assert ids(await backend.maintenance.find_system(ItemFilter(created_before=cutoff))) == ["i1"]
    assert ids(await backend.maintenance.find_system(ItemFilter(org_id="org2"))) == []
    assert ids(
        await backend.maintenance.find_system(ItemFilter(scope_level=ScopeLevel.PROJECT))
    ) == ["i2", "i3", "i4"]
    assert ids(
        await backend.maintenance.find_system(ItemFilter(scope_level=ScopeLevel.PRIVATE))
    ) == ["i1"]
    assert ids(await backend.maintenance.find_system(ItemFilter(updated_before=cutoff))) == ["i1"]
    seen, after = [], None
    while page := await backend.maintenance.find_system(ItemFilter(), after_id=after, limit=3):
        seen += ids(page)
        after = page[-1].id
    assert seen == ["i1", "i2", "i3", "i4"]
    found = (await backend.maintenance.find_system(ItemFilter(author_id="alice")))[1]
    assert found.tags == {"x"}  # tags are loaded


async def test_count_by_source_is_per_org(backend):
    c = backend.clock
    for n, who in enumerate((ALICE, ALICE, BOB)):
        item = make_item(c, who, P1, item_id=f"s{n}")
        await backend.items.create(who, replace(item, source="blob:abc:1"))
    assert await backend.maintenance.count_by_source("org1", "blob:abc:1") == 3
    assert await backend.maintenance.count_by_source("org1", "blob:zzz:1") == 0
    assert await backend.maintenance.count_by_source("org2", "blob:abc:1") == 0
    await backend.maintenance.purge_system("org1", "s0")
    assert await backend.maintenance.count_by_source("org1", "blob:abc:1") == 2


async def test_edges_and_promotions_can_be_removed_per_item(backend):
    now = backend.clock.now()
    for src, dst in [("a", "b"), ("b", "c"), ("c", "d")]:
        await backend.edges.add(Edge("org1", src, dst, "related", "alice", now))
    await backend.edges.add(Edge("org2", "b", "z", "related", "zoe", now))
    assert await backend.edges.remove_item("org1", "b") == 2
    assert [e.key for e in await backend.edges.incident("org1", ["a", "b", "c", "d"])] == [
        ("c", "d", "related")
    ]
    assert len(await backend.edges.incident("org2", ["b"])) == 1  # other tenants untouched
    assert await backend.edges.remove_item("org1", "b") == 0


async def test_count_system_excludes_archived_and_filters_by_org(backend):
    items = await seed(backend)
    assert await backend.maintenance.count_system() == 4
    assert await backend.maintenance.count_system("org1") == 4
    assert await backend.maintenance.count_system("org2") == 0
    await backend.items.update(ALICE, items["a_proj"].id, ItemPatch(status=Status.ARCHIVED), 1)
    assert await backend.maintenance.count_system() == 3
    assert await backend.maintenance.count_system(include_archived=True) == 4


async def test_usage_per_author_excludes_archived_and_other_orgs(backend):
    c = backend.clock
    await backend.items.create(
        ALICE, make_item(c, ALICE, P1, item_id="u1", title="ab", body="cdef")
    )
    c.advance(seconds=10)
    await backend.items.create(ALICE, make_item(c, ALICE, P1, item_id="u2", title="x", body="y"))
    await backend.items.create(
        BOB, make_item(c, BOB, P1, item_id="u3", title="bobtitle", body="bobbody!")
    )
    await backend.items.create(
        ALICE, make_item(c, ALICE, P1, item_id="u4", title="gone", body="gone")
    )
    await backend.items.update(ALICE, "u4", ItemPatch(status=Status.ARCHIVED), 1)
    rows = await backend.maintenance.usage("org1")
    assert [(u.author_id, u.items, u.chars) for u in rows] == [("bob", 1, 16), ("alice", 2, 8)]
    assert rows[1].last_at == c.now()  # alice's newest item (u2) was written at the last clock tick
    assert [u.author_id for u in await backend.maintenance.usage("org1", "alice")] == ["alice"]
    assert await backend.maintenance.usage("org1", "nobody") == []
    assert await backend.maintenance.usage("org2") == []


async def test_set_status_system_bumps_the_revision_and_respects_the_org_and_revision(backend):
    from hive_mcp.domain.errors import Conflict, NotFound

    await seed(backend)
    changed = await backend.items.set_status_system("org1", "i2", Status.ARCHIVED, 1)
    assert changed.status is Status.ARCHIVED and changed.revision == 2
    assert (await backend.items.get_system("org1", "i2")).status is Status.ARCHIVED
    with pytest.raises(Conflict):
        await backend.items.set_status_system("org1", "i2", Status.ACTIVE, 1)  # stale revision
    with pytest.raises(NotFound):
        await backend.items.set_status_system("org2", "i1", Status.ARCHIVED, 1)  # other tenant
    with pytest.raises(NotFound):
        await backend.items.set_status_system("org1", "nope", Status.ARCHIVED, 1)
    flt = ItemFilter(exclude_archived=True)
    assert [i.id for i in await backend.maintenance.find_system(flt)] == ["i1", "i3", "i4"]


async def test_find_system_can_exclude_private_and_start_at_a_change_time(backend):
    await seed(backend)
    ids = lambda rows: [i.id for i in rows]  # noqa: E731
    shared = ItemFilter(exclude_private=True)
    assert ids(await backend.maintenance.find_system(shared)) == ["i2", "i3", "i4"]
    assert ids(await backend.maintenance.find_system(shared, after_id="i2", limit=1)) == ["i3"]
    cutoff = backend.clock.now() - timedelta(seconds=50)  # i1 is 100 s older than the rest
    assert ids(await backend.maintenance.find_system(ItemFilter(updated_since=cutoff))) == [
        "i2",
        "i3",
        "i4",
    ]
    both = ItemFilter(updated_since=cutoff, exclude_private=True, org_id="org1")
    assert ids(await backend.maintenance.find_system(both)) == ["i2", "i3", "i4"]
    late = ItemFilter(updated_since=backend.clock.now() + timedelta(seconds=1))
    assert await backend.maintenance.find_system(late) == []
