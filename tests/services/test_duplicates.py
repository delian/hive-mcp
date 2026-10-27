import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.services.duplicates import DuplicateFinder
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, CAROL


async def note(stack, who, title, body):
    return (await stack.knowledge.remember(who, RememberRequest(title=title, body=body))).item


def finder(stack):
    return DuplicateFinder(stack.backend.items, stack.curation)


async def test_groups_of_duplicates_are_found_and_unrelated_notes_are_not(stack):
    a = await note(
        stack, ALICE, "Radar range", "the radar range equation relates power and distance"
    )
    b = await note(
        stack, ALICE, "Radar range copy", "the radar range equation relates power and distance"
    )
    c = await note(
        stack, ALICE, "Radar again", "the radar range equation relates power and distance"
    )
    x = await note(stack, ALICE, "Cake", "chocolate butter flour sugar oven")
    y = await note(stack, ALICE, "Cake two", "chocolate butter flour sugar oven")
    await note(stack, ALICE, "Lonely", "quantum entanglement of pelicans")
    sweep = await finder(stack).sweep(ALICE, threshold=0.9)
    assert [g.ids for g in sweep.groups] == [
        tuple(sorted([a.id, b.id, c.id])),
        tuple(sorted([x.id, y.id])),
    ]
    assert sweep.next_cursor is None and sweep.scanned == 6
    assert all(g.best_score >= 0.9 for g in sweep.groups)


async def test_only_what_the_caller_can_see_and_edit_is_considered(stack):
    await note(stack, ALICE, "mine", "shared words about antennas and gain")
    await note(stack, CAROL, "hers", "shared words about antennas and gain")  # private to carol
    assert (await finder(stack).sweep(ALICE)).groups == []


async def test_archived_items_are_ignored_and_the_sweep_pages(stack):
    a = await note(
        stack, ALICE, "a", "unique pelican words about migration routes and nesting habits"
    )
    b = await note(
        stack, ALICE, "b", "unique pelican words about migration routes and nesting habits"
    )
    await note(stack, ALICE, "c", "other thing entirely about gardens")
    f = finder(stack)
    first = await f.sweep(ALICE, scan=1, threshold=0.85)
    assert first.scanned == 1 and first.next_cursor
    seen = {tuple(g.ids) for g in first.groups}
    rest = await f.sweep(ALICE, after=first.next_cursor, scan=10, threshold=0.85)
    assert rest.next_cursor is None and rest.scanned == 2
    seen |= {tuple(g.ids) for g in rest.groups}
    assert seen == {tuple(sorted([a.id, b.id]))}
    await stack.knowledge.update(ALICE, b.id, ItemPatch(status=Status.ARCHIVED), 1)
    assert (await f.sweep(ALICE, threshold=0.85)).groups == []


@pytest.mark.parametrize("kw", [{"scan": 0}, {"scan": 201}, {"threshold": 0.4}, {"threshold": 1.1}])
async def test_bad_arguments_are_refused(stack, kw):
    with pytest.raises(ValidationError):
        await finder(stack).sweep(ALICE, **kw)
