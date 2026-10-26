import pytest

from hive_mcp.domain.errors import Conflict, NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemPatch, Kind
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL

P1 = Scope(ScopeLevel.PROJECT, "p1")


async def start(stack, who=ALICE, scope=None, **kw):
    req = RememberRequest(
        title=kw.get("title", "Pulse plan"), body=kw.get("body", "line one\nline two\nline three"),
        scope=scope, tags=tuple(kw.get("tags", ())),
    )  # fmt: skip
    return (await stack.knowledge.remember(who, req)).item


async def test_history_shows_each_edit_and_the_diff_names_the_change(stack):
    item = await start(stack, tags=["a"])
    v2 = await stack.knowledge.update(
        ALICE, item.id, ItemPatch(body="line one\nline TWO\nline three"), 1
    )
    await stack.knowledge.update(ALICE, item.id, ItemPatch(title="Pulse plan v2"), v2.revision)
    history = await stack.knowledge.history(ALICE, item.id)
    assert [(v.revision, v.title) for v in history] == [
        (3, "Pulse plan v2"),
        (2, "Pulse plan"),
        (1, "Pulse plan"),
    ]
    diff = await stack.knowledge.diff(ALICE, item.id, 1, 2)
    assert (
        "-line two" in diff
        and "+line TWO" in diff
        and "revision 1" in diff
        and "revision 2" in diff
    )
    assert "+# Pulse plan v2" in await stack.knowledge.diff(ALICE, item.id, 2)  # to = current
    assert await stack.knowledge.diff(ALICE, item.id, 3) == "(no difference)"
    with pytest.raises(NotFound):
        await stack.knowledge.diff(ALICE, item.id, 1, 99)


async def test_restore_creates_a_new_revision_with_the_old_content(stack):
    item = await start(stack, tags=["a", "b"])
    v2 = await stack.knowledge.update(
        ALICE,
        item.id,
        ItemPatch(
            body="rewritten",
            title="New title",
            add_tags=frozenset({"c"}),
            remove_tags=frozenset({"a"}),
        ),
        1,
    )
    restored = await stack.knowledge.restore(ALICE, item.id, 1, v2.revision)
    assert (
        restored.revision == 3
        and restored.title == "Pulse plan"
        and restored.body.startswith("line one")
    )
    assert restored.tags == {"a", "b"}
    assert len(await stack.knowledge.history(ALICE, item.id)) == 3  # nothing was rewritten or lost
    found = await stack.consult.consult(
        ALICE, ConsultRequest("line three")
    )  # search follows the restore
    assert [m.item_id for m in found.primary] == [item.id]
    with pytest.raises(ValidationError, match="already matches"):
        await stack.knowledge.restore(ALICE, item.id, 3, restored.revision)
    with pytest.raises(Conflict):
        await stack.knowledge.restore(ALICE, item.id, 2, 1)  # stale expected revision


async def test_restore_needs_edit_rights_and_a_visible_version(stack):
    item = await start(stack, scope=P1)
    await stack.knowledge.update(ALICE, item.id, ItemPatch(body="changed"), 1)
    with pytest.raises(PermissionDenied):
        await stack.knowledge.restore(BOB, item.id, 1, 2)  # a project mate who is not the author
    with pytest.raises(NotFound):
        await stack.knowledge.history(CAROL, item.id)


async def test_private_drafts_do_not_leak_through_history_after_sharing(stack):
    item = await start(stack, body="private draft with a secret plan")
    v2 = await stack.knowledge.update(ALICE, item.id, ItemPatch(body="clean public version"), 1)
    await stack.knowledge.move(ALICE, item.id, P1, Visibility.SCOPE, v2.revision)
    assert [v.revision for v in await stack.knowledge.history(BOB, item.id)] == [3]
    with pytest.raises(NotFound):
        await stack.knowledge.version(BOB, item.id, 1)
    with pytest.raises(NotFound):
        await stack.knowledge.diff(BOB, item.id, 1)
    assert len(await stack.knowledge.history(ALICE, item.id)) == 3
    bob_editor = __import__("hive_mcp.domain.principal", fromlist=["x"]).Principal(
        "eddie",
        "org1",
        __import__("hive_mcp.domain.principal", fromlist=["x"]).Role.EDITOR,
        projects=frozenset({"p1"}),
    )
    with pytest.raises(NotFound):
        await stack.knowledge.restore(
            bob_editor, item.id, 1, 3
        )  # cannot restore what he cannot read


async def test_history_limit_and_availability(stack):
    item = await start(stack)
    for rev in range(1, 6):
        await stack.knowledge.update(ALICE, item.id, ItemPatch(body=f"body {rev}"), rev)
    assert len(await stack.knowledge.history(ALICE, item.id, limit=2)) == 2
    for bad in (0, 52):
        with pytest.raises(ValidationError):
            await stack.knowledge.history(ALICE, item.id, limit=bad)
    stack.knowledge._history = None
    with pytest.raises(ValidationError, match="not available"):
        await stack.knowledge.history(ALICE, item.id)


async def test_kind_changes_are_restorable(stack):
    item = await start(stack)
    v2 = await stack.knowledge.update(ALICE, item.id, ItemPatch(kind=Kind.DECISION), 1)
    assert (await stack.knowledge.restore(ALICE, item.id, 1, v2.revision)).kind is Kind.NOTE
