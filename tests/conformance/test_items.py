from dataclasses import replace

import pytest

from hive_mcp.domain.access import Tier
from hive_mcp.domain.errors import (
    Conflict,
    IdempotencyConflict,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from hive_mcp.domain.item import Cursor, ItemPatch, ItemQuery, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from tests.conformance.factory import (
    ALICE,
    ALICE_PRIVATE,
    BOB,
    CAROL,
    EDDIE,
    ORG1,
    P1,
    P2,
    VIVI,
    ZOE,
    make_item,
)


async def test_create_get_roundtrip(backend):
    item = make_item(backend.clock)
    result = await backend.items.create(ALICE, item)
    view = await backend.items.get(ALICE, item.id)
    assert result.created and view.item == item and view.tier is Tier.PRIMARY


async def test_invisible_items_look_missing(backend):
    private = make_item(backend.clock, scope=ALICE_PRIVATE)
    project2 = make_item(backend.clock, author=CAROL, scope=P2)
    for item, author in ((private, ALICE), (project2, CAROL)):
        await backend.items.create(author, item)
    with pytest.raises(NotFound):
        await backend.items.get(BOB, private.id)  # same project, but private to alice
    with pytest.raises(NotFound):
        await backend.items.get(ALICE, project2.id)  # other project, not shared
    with pytest.raises(NotFound):
        await backend.items.get(ZOE, private.id)  # other tenant
    with pytest.raises(NotFound):
        await backend.items.get(ALICE, "does-not-exist")


async def test_org_visible_items_are_referenced_for_non_members(backend):
    item = make_item(backend.clock, author=CAROL, scope=P2, visibility=Visibility.ORG)
    await backend.items.create(CAROL, item)
    assert (await backend.items.get(ALICE, item.id)).tier is Tier.REFERENCED
    assert (await backend.items.get(CAROL, item.id)).tier is Tier.PRIMARY
    with pytest.raises(NotFound):
        await backend.items.get(ZOE, item.id)  # never across tenants


async def test_create_permissions(backend):
    with pytest.raises(PermissionDenied):  # viewers are read-only
        await backend.items.create(VIVI, make_item(backend.clock, author=VIVI))
    with pytest.raises(PermissionDenied):  # not a member of the target scope
        await backend.items.create(ALICE, make_item(backend.clock, author=ALICE, scope=P2))
    with pytest.raises(PermissionDenied):  # cannot impersonate another author
        await backend.items.create(ALICE, make_item(backend.clock, author=BOB))


async def test_idempotent_create(backend):
    item = make_item(backend.clock)
    first = await backend.items.create(ALICE, item, idempotency_key="k1")
    replay = await backend.items.create(ALICE, item, idempotency_key="k1")
    assert first.created and not replay.created and replay.item == item
    other = make_item(backend.clock, title="Different")
    with pytest.raises(IdempotencyConflict):
        await backend.items.create(ALICE, other, idempotency_key="k1")
    # keys are per author: bob's k1 is independent
    bobs = make_item(backend.clock, author=BOB)
    assert (await backend.items.create(BOB, bobs, idempotency_key="k1")).created


async def test_update_revisions_and_conflicts(backend):
    item = make_item(backend.clock)
    await backend.items.create(ALICE, item)
    backend.clock.advance(seconds=5)
    updated = await backend.items.update(ALICE, item.id, ItemPatch(title="New"), 1)
    assert updated.revision == 2 and updated.title == "New" and updated.updated_at > item.updated_at
    with pytest.raises(Conflict):
        await backend.items.update(ALICE, item.id, ItemPatch(title="Stale"), 1)


async def test_update_permissions(backend):
    item = make_item(backend.clock)
    await backend.items.create(ALICE, item)
    with pytest.raises(PermissionDenied):  # bob can read, but members only edit their own
        await backend.items.update(BOB, item.id, ItemPatch(title="x"), 1)
    assert (
        await backend.items.update(EDDIE, item.id, ItemPatch(title="By editor"), 1)
    ).revision == 2
    org_item = make_item(backend.clock, author=CAROL, scope=P2, visibility=Visibility.ORG)
    await backend.items.create(CAROL, org_item)
    with pytest.raises(PermissionDenied):  # referenced = readable but never writable
        await backend.items.update(ALICE, org_item.id, ItemPatch(title="x"), 1)


async def test_tag_patches_are_additive(backend):
    item = make_item(backend.clock, tags=frozenset({"domain/rf"}))
    await backend.items.create(ALICE, item)
    a = await backend.items.update(
        ALICE, item.id, ItemPatch(add_tags=frozenset({"kind/formula"})), 1
    )
    b = await backend.items.update(EDDIE, item.id, ItemPatch(add_tags=frozenset({"project/x"})), 2)
    assert b.tags == {"domain/rf", "kind/formula", "project/x"} and a.revision == 2
    c = await backend.items.update(
        ALICE, item.id, ItemPatch(remove_tags=frozenset({"domain/rf"})), 3
    )
    assert c.tags == {"kind/formula", "project/x"}


async def test_relocate_promotes_and_checks_membership(backend):
    item = make_item(backend.clock, scope=ALICE_PRIVATE)
    await backend.items.create(ALICE, item)
    moved = await backend.items.relocate(ALICE, item.id, P1, Visibility.SCOPE, 1)
    assert moved.scope == P1 and moved.revision == 2
    assert (await backend.items.get(BOB, item.id)).tier is Tier.PRIMARY  # bob is in p1 now
    with pytest.raises(PermissionDenied):  # alice is not in p2
        await backend.items.relocate(ALICE, item.id, P2, Visibility.SCOPE, 2)
    with pytest.raises(Conflict):
        await backend.items.relocate(ALICE, item.id, ORG1, Visibility.ORG, 1)
    with pytest.raises(ValidationError):  # private + org-visible is never allowed
        await backend.items.relocate(ALICE, item.id, ALICE_PRIVATE, Visibility.ORG, 2)


async def test_relocate_cannot_target_other_orgs_scope(backend):
    item = make_item(backend.clock)
    await backend.items.create(ALICE, item)
    with pytest.raises(PermissionDenied):
        await backend.items.relocate(
            ALICE, item.id, Scope(ScopeLevel.ORG, "org2"), Visibility.SCOPE, 1
        )


async def test_get_many_drops_invisible_and_keeps_order(backend):
    mine = [make_item(backend.clock) for _ in range(3)]
    theirs = make_item(backend.clock, author=CAROL, scope=P2)
    for it in mine:
        await backend.items.create(ALICE, it)
    await backend.items.create(CAROL, theirs)
    ids = [mine[2].id, theirs.id, "missing", mine[0].id]
    views = await backend.items.get_many(ALICE, ids)
    assert [v.item.id for v in views] == [mine[2].id, mine[0].id]


async def test_list_pagination_is_complete_with_equal_timestamps(backend):
    created = [make_item(backend.clock) for _ in range(25)]  # identical created_at
    for it in created:
        await backend.items.create(ALICE, it)
    seen, cursor = [], None
    while True:
        query = ItemQuery(limit=10, after=Cursor.decode(cursor) if cursor else None)
        page = await backend.items.list(ALICE, query)
        seen += [v.item.id for v in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert sorted(seen) == sorted(i.id for i in created) and len(seen) == len(set(seen))


async def test_list_filters(backend):
    a = make_item(backend.clock, kind=Kind.FORMULA, tags=frozenset({"domain/rf", "x"}))
    b = make_item(backend.clock, kind=Kind.NOTE, tags=frozenset({"domain/rf"}))
    c = make_item(backend.clock, author=CAROL, scope=P2, visibility=Visibility.ORG)
    for it, who in ((a, ALICE), (b, ALICE), (c, CAROL)):
        await backend.items.create(who, it)
    await backend.items.update(ALICE, b.id, ItemPatch(status=Status.ARCHIVED), 1)

    def ids(page):
        return {v.item.id for v in page.items}

    assert ids(await backend.items.list(ALICE, ItemQuery())) == {a.id, c.id}  # archived hidden
    assert ids(await backend.items.list(ALICE, ItemQuery(include_referenced=False))) == {a.id}
    assert ids(await backend.items.list(ALICE, ItemQuery(kinds=frozenset({Kind.FORMULA})))) == {
        a.id
    }
    assert ids(
        await backend.items.list(ALICE, ItemQuery(tags_all=frozenset({"x", "domain/rf"})))
    ) == {a.id}
    all_statuses = ItemQuery(statuses=frozenset(Status))
    assert ids(await backend.items.list(ALICE, all_statuses)) == {a.id, b.id, c.id}
    assert ids(await backend.items.list(ZOE, all_statuses)) == set()


async def test_failed_create_does_not_poison_its_idempotency_key(backend):
    first = make_item(backend.clock, item_id="dup")
    await backend.items.create(ALICE, first)
    clash = make_item(backend.clock, title="Other", item_id="dup")
    with pytest.raises(Conflict):  # id already taken
        await backend.items.create(ALICE, clash, idempotency_key="k")
    fresh = make_item(backend.clock, title="Other", item_id="new")
    result = await backend.items.create(
        ALICE, fresh, idempotency_key="k"
    )  # key must still be usable
    assert result.created and result.item.id == "new"
    replay = await backend.items.create(ALICE, fresh, idempotency_key="k")
    assert not replay.created and replay.item.id == "new"


@pytest.mark.parametrize("key", ["", "x" * 201, "bad\x00key"])
async def test_bad_idempotency_keys_are_rejected_everywhere(backend, key):
    with pytest.raises(ValidationError):
        await backend.items.create(ALICE, make_item(backend.clock), idempotency_key=key)


async def test_long_unicode_and_control_text_roundtrips(backend):
    body = "Радар 雷达 🛰️ " + "x" * 5000 + "\n\ttab and 'quotes' \"double\" ; DROP TABLE items; --"
    item = make_item(backend.clock, title="Тест 测试", body=body)
    await backend.items.create(ALICE, item)
    assert (await backend.items.get(ALICE, item.id)).item == item


async def test_system_listing_is_org_scoped_ordered_and_paginated(backend):
    made = [make_item(backend.clock, scope=ALICE_PRIVATE) for _ in range(5)]
    for item in made:
        await backend.items.create(ALICE, item)
    want = sorted(i.id for i in made)
    assert await backend.items.list_ids_system("org1", None, 100) == want
    assert await backend.items.list_ids_system("org2", None, 100) == []
    seen, after = [], None
    while page := await backend.items.list_ids_system("org1", after, 2):
        seen += page
        after = page[-1]
    assert seen == want


async def test_system_read_ignores_user_acl_but_never_crosses_orgs(backend):
    private = make_item(backend.clock, scope=ALICE_PRIVATE)
    await backend.items.create(ALICE, private)
    assert await backend.items.get_system("org1", private.id) == private  # no principal needed
    assert await backend.items.get_system("org2", private.id) is None  # wrong tenant
    assert await backend.items.get_system("org1", "missing") is None


async def test_tag_counts_are_visibility_scoped_ordered_and_prefix_filtered(backend):
    c = backend.clock
    rows = [
        (ALICE, ALICE_PRIVATE, frozenset({"domain/rf", "kind/note"})),
        (ALICE, P1, frozenset({"domain/rf", "project/x"})),
        (BOB, P1, frozenset({"domain/rf", "domain/sonar"})),
        (CAROL, P2, frozenset({"domain/rf", "secret/carol"})),  # alice cannot see it
    ]
    for author, scope, tags in rows:
        await backend.items.create(author, make_item(c, author, scope, tags=tags))
    await backend.items.create(  # archived items do not count
        ALICE,
        replace(make_item(c, ALICE, P1, tags=frozenset({"domain/rf"})), status=Status.ARCHIVED),
    )
    counts = await backend.items.tag_counts(ALICE)
    assert counts[0] == ("domain/rf", 3)
    assert dict(counts) == {
        "domain/rf": 3, "kind/note": 1, "project/x": 1, "domain/sonar": 1,
    }  # fmt: skip
    assert [t for t, _ in counts] == [
        "domain/rf",
        "domain/sonar",
        "kind/note",
        "project/x",
    ]  # ties by name
    assert await backend.items.tag_counts(ALICE, prefix="domain/") == [
        ("domain/rf", 3),
        ("domain/sonar", 1),
    ]
    assert await backend.items.tag_counts(ALICE, prefix="domain/rf") == [("domain/rf", 3)]
    assert await backend.items.tag_counts(ALICE, limit=2) == counts[:2]
    assert await backend.items.tag_counts(ALICE, prefix="nope") == []
    assert await backend.items.tag_counts(ZOE) == []  # other tenant sees nothing
    assert "secret/carol" in dict(await backend.items.tag_counts(CAROL))
    assert await backend.items.tag_counts(ALICE, prefix="%") == []  # no wildcard semantics


async def test_browsing_can_order_by_last_change_with_a_stable_cursor(backend):

    made = []
    for n in range(5):
        item = make_item(backend.clock, ALICE, P1, title=f"item {n}")
        await backend.items.create(ALICE, item)
        made.append(item)
        backend.clock.advance(minutes=1)
    backend.clock.advance(hours=1)
    await backend.items.update(ALICE, made[1].id, ItemPatch(title="edited early"), 1)
    backend.clock.advance(minutes=5)
    await backend.items.update(ALICE, made[3].id, ItemPatch(title="edited late"), 1)
    by_created = await backend.items.list(ALICE, ItemQuery(limit=10))
    assert [v.item.id for v in by_created.items] == [m.id for m in reversed(made)]
    by_updated = await backend.items.list(ALICE, ItemQuery(limit=10, by_updated=True))
    assert [v.item.id for v in by_updated.items][:2] == [
        made[3].id,
        made[1].id,
    ]  # latest change first
    seen, cursor = [], None
    while True:  # paging by update time visits each item exactly once
        page = await backend.items.list(ALICE, ItemQuery(limit=2, by_updated=True, after=cursor))
        seen += [v.item.id for v in page.items]
        if not page.next_cursor:
            break
        cursor = Cursor.decode(page.next_cursor)
    assert seen == [v.item.id for v in by_updated.items] and len(set(seen)) == 5
