import pytest

from hive_mcp.domain.errors import NotFound
from hive_mcp.domain.history import KEEP_VERSIONS
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.scope import Visibility
from tests.conformance.factory import ALICE, ALICE_PRIVATE, BOB, CAROL, EDDIE, P1, make_item


async def edit(backend, who, item_id, rev, **patch):
    backend.clock.advance(seconds=1)
    return await backend.items.update(who, item_id, ItemPatch(**patch), rev)


async def test_every_update_keeps_the_replaced_version(backend):
    item = make_item(backend.clock, ALICE, P1, title="T1", body="first", tags=frozenset({"a"}))
    await backend.items.create(ALICE, item)
    r2 = await edit(backend, ALICE, item.id, 1, body="second", add_tags=frozenset({"b"}))
    await edit(backend, EDDIE, item.id, 2, title="T3", remove_tags=frozenset({"a"}))
    versions = await backend.history.versions(BOB, item.id)
    assert [v.revision for v in versions] == [3, 2, 1]
    cur, mid, first = versions
    assert (cur.current, mid.current, first.current) == (True, False, False)
    assert (first.title, first.body, first.tags) == ("T1", "first", {"a"})
    assert (mid.title, mid.body, mid.tags) == ("T1", "second", {"a", "b"})
    assert (cur.title, cur.tags) == ("T3", {"b"})
    assert first.replaced_by == "alice" and mid.replaced_by == "eddie"  # who made the next change
    assert first.replaced_at == r2.updated_at and mid.written_at == r2.updated_at
    assert first.item_author == "alice" and first.written_at < mid.written_at < cur.written_at


async def test_moving_an_item_is_a_version_and_old_private_versions_stay_private(backend):
    item = make_item(backend.clock, ALICE, ALICE_PRIVATE, body="draft with private details")
    await backend.items.create(ALICE, item)
    await edit(backend, ALICE, item.id, 1, body="polished for sharing")
    moved = await backend.items.relocate(ALICE, item.id, P1, Visibility.SCOPE, 2)
    assert moved.revision == 3
    mine = await backend.history.versions(ALICE, item.id)
    assert [(v.revision, v.scope.level.name) for v in mine] == [
        (3, "PROJECT"),
        (2, "PRIVATE"),
        (1, "PRIVATE"),
    ]
    theirs = await backend.history.versions(BOB, item.id)  # a project mate
    assert [v.revision for v in theirs] == [3]  # earlier versions were written while private
    with pytest.raises(NotFound):
        await backend.history.version(BOB, item.id, 1)
    assert (await backend.history.version(ALICE, item.id, 1)).body == "draft with private details"


async def test_system_moves_are_recorded_too(backend):
    item = make_item(backend.clock, ALICE, ALICE_PRIVATE)
    await backend.items.create(ALICE, item)
    await backend.items.relocate_system("org1", item.id, P1, Visibility.SCOPE, 1)
    (cur, old) = await backend.history.versions(ALICE, item.id)
    assert old.replaced_by == "system" and old.scope.level.name == "PRIVATE" and cur.revision == 2


async def test_invisible_items_have_no_history_and_unknown_revisions_are_not_found(backend):
    item = make_item(backend.clock, ALICE, ALICE_PRIVATE)
    await backend.items.create(ALICE, item)
    for who in (BOB, CAROL):
        with pytest.raises(NotFound):
            await backend.history.versions(who, item.id)
    with pytest.raises(NotFound):
        await backend.history.versions(ALICE, "missing")
    with pytest.raises(NotFound):
        await backend.history.version(ALICE, item.id, 99)
    assert [v.revision for v in await backend.history.versions(ALICE, item.id)] == [
        1
    ]  # nothing replaced yet


async def test_only_the_newest_versions_are_kept_and_limit_applies(backend):
    item = make_item(backend.clock, ALICE, P1, body="v1")
    await backend.items.create(ALICE, item)
    total = KEEP_VERSIONS + 5
    for rev in range(1, total):
        await edit(backend, ALICE, item.id, rev, body=f"v{rev + 1}")
    versions = await backend.history.versions(ALICE, item.id, limit=1000)
    assert len(versions) == KEEP_VERSIONS + 1  # current + the newest kept ones
    assert versions[0].revision == total and versions[-1].revision == total - KEEP_VERSIONS
    assert len(await backend.history.versions(ALICE, item.id, limit=3)) == 3
    with pytest.raises(NotFound):
        await backend.history.version(ALICE, item.id, 1)  # dropped


async def test_failed_updates_leave_no_phantom_version(backend):
    item = make_item(backend.clock, ALICE, P1)
    await backend.items.create(ALICE, item)
    with pytest.raises(Exception):  # noqa: B017 - stale revision
        await backend.items.update(ALICE, item.id, ItemPatch(body="x"), 7)
    assert [v.revision for v in await backend.history.versions(ALICE, item.id)] == [1]


async def test_purge_takes_the_history_with_it(backend):
    item = make_item(backend.clock, ALICE, P1)
    await backend.items.create(ALICE, item)
    await edit(backend, ALICE, item.id, 1, body="two")
    await backend.maintenance.purge_system("org1", item.id)
    again = make_item(backend.clock, ALICE, P1, item_id=item.id)  # same id reused
    await backend.items.create(ALICE, again)
    assert [v.revision for v in await backend.history.versions(ALICE, item.id)] == [1]
