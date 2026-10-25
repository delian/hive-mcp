import pytest

from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL, ROOT

P1 = Scope(ScopeLevel.PROJECT, "p1")
P2 = Scope(ScopeLevel.PROJECT, "p2")


async def note(stack, who, title, scope=None, **kw):  # kw: vis, body
    req = RememberRequest(
        title=title,
        body=kw.get("body", "some body text"),
        scope=scope,
        visibility=kw.get("vis", Visibility.SCOPE),
    )
    return (await stack.knowledge.remember(who, req)).item


def titles(result):
    return sorted(n.view.item.title for n in result.nodes)


async def test_link_and_traverse_with_depth_and_direction_agnostic(stack):
    a, b, c = [await note(stack, ALICE, t) for t in "abc"]
    assert await stack.graph.link(ALICE, a.id, b.id, "Derived From")
    assert not await stack.graph.link(ALICE, a.id, b.id, "derived_from")  # idempotent
    await stack.graph.link(ALICE, c.id, b.id, "supports")
    one = await stack.graph.neighbours(ALICE, a.id)
    assert titles(one) == ["a", "b"] and one.nodes[0].view.item.id == a.id
    assert [e.relation for e in one.edges] == ["derived_from"]
    two = await stack.graph.neighbours(ALICE, a.id, depth=2)
    assert titles(two) == ["a", "b", "c"] and {n.depth for n in two.nodes} == {0, 1, 2}
    back = await stack.graph.neighbours(ALICE, b.id)  # incoming edges are followed too
    assert titles(back) == ["a", "b", "c"]
    only = await stack.graph.neighbours(ALICE, b.id, relations=frozenset({"supports"}))
    assert titles(only) == ["b", "c"]


async def test_cycles_terminate(stack):
    a, b = await note(stack, ALICE, "a"), await note(stack, ALICE, "b")
    await stack.graph.link(ALICE, a.id, b.id)
    await stack.graph.link(ALICE, b.id, a.id)
    res = await stack.graph.neighbours(ALICE, a.id, depth=3)
    assert titles(res) == ["a", "b"] and len(res.edges) == 2


async def test_edges_never_reveal_items_the_caller_cannot_see(stack):
    mine = await note(stack, ALICE, "mine", P1, body="shared in p1")
    secret = await note(stack, ALICE, "secret plan")  # private
    await stack.graph.link(ALICE, mine.id, secret.id, "derived_from")
    bob = await stack.graph.neighbours(BOB, mine.id)  # bob sees p1 but not the private note
    assert titles(bob) == ["mine"] and bob.edges == []
    with pytest.raises(NotFound):
        await stack.graph.neighbours(BOB, secret.id)
    with pytest.raises(NotFound):
        await stack.graph.link(BOB, mine.id, secret.id)  # cannot link to what he cannot see
    assert titles(await stack.graph.neighbours(ALICE, mine.id)) == ["mine", "secret plan"]


async def test_other_groups_knowledge_can_be_referenced_and_is_labelled(stack):
    theirs = await note(stack, ROOT, "root finding", P2, vis=Visibility.ORG)
    mine = await note(stack, ALICE, "my extension")
    await stack.graph.link(ALICE, mine.id, theirs.id, "derived_from")
    res = await stack.graph.neighbours(ALICE, mine.id)
    external = {n.view.item.title: n.view.tier.value for n in res.nodes}
    assert external == {"my extension": "primary", "root finding": "referenced"}
    # Carol cannot see Alice's private note, so she sees no link at all.
    assert (await stack.graph.neighbours(CAROL, theirs.id)).edges == []


async def test_link_needs_write_access_to_the_source(stack):
    bobs = await note(stack, BOB, "bobs", P1)
    other = await note(stack, ALICE, "other", P1)
    with pytest.raises(PermissionDenied):
        await stack.graph.link(
            Principal("vi", "org1", Role.VIEWER, projects=frozenset({"p1"})), bobs.id, other.id
        )
    editor = Principal("eddie", "org1", Role.EDITOR, projects=frozenset({"p1"}))
    assert await stack.graph.link(
        editor, bobs.id, other.id
    )  # editors may link any item in their scopes


async def test_unlink_rules(stack):
    a, b = await note(stack, ALICE, "a", P1), await note(stack, ALICE, "b", P1)
    await stack.graph.link(ALICE, a.id, b.id)
    with pytest.raises(PermissionDenied):
        await stack.graph.unlink(BOB, a.id, b.id, "related")  # member, not the author nor editor
    with pytest.raises(NotFound):
        await stack.graph.unlink(ALICE, a.id, b.id, "supports")
    await stack.graph.unlink(ALICE, a.id, b.id, "related")
    assert (await stack.graph.neighbours(ALICE, a.id)).edges == []
    actions = [r.event.action for r in await stack.backend.audit.list("org1")]
    assert "edge.add" in actions and "edge.remove" in actions


async def test_moving_or_archiving_an_item_changes_who_sees_the_link(stack):
    a, b = await note(stack, ALICE, "a", P1), await note(stack, ALICE, "b", P1)
    await stack.graph.link(ALICE, a.id, b.id)
    assert titles(await stack.graph.neighbours(BOB, a.id)) == ["a", "b"]
    moved = await stack.knowledge.move(
        ALICE, b.id, Scope(ScopeLevel.PRIVATE, "alice"), Visibility.SCOPE, b.revision
    )
    assert titles(await stack.graph.neighbours(BOB, a.id)) == ["a"]  # b became private
    await stack.knowledge.move(ALICE, b.id, P1, Visibility.SCOPE, moved.revision)
    b2 = await stack.backend.items.get_system("org1", b.id)
    await stack.knowledge.update(ALICE, b.id, ItemPatch(status=Status.ARCHIVED), b2.revision)
    assert titles(await stack.graph.neighbours(BOB, a.id)) == ["a"]  # archived nodes are hidden


async def test_limits_and_validation(stack):
    hub = await note(stack, ALICE, "hub")
    for i in range(6):
        await stack.graph.link(ALICE, hub.id, (await note(stack, ALICE, f"n{i}")).id)
    res = await stack.graph.neighbours(ALICE, hub.id, max_nodes=4)
    assert len(res.nodes) == 4 and res.truncated
    for bad in ({"depth": 0}, {"depth": 4}, {"max_nodes": 0}, {"max_nodes": 101}):
        with pytest.raises(ValidationError):
            await stack.graph.neighbours(ALICE, hub.id, **bad)
    with pytest.raises(ValidationError):
        await stack.graph.link(ALICE, hub.id, hub.id)  # self link
    with pytest.raises(ValidationError):
        await stack.graph.link(ALICE, hub.id, (await note(stack, ALICE, "x")).id, "Bad Relation!")
