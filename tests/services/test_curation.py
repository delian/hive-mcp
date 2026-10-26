import pytest

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL

P1 = Scope(ScopeLevel.PROJECT, "p1")


async def note(stack, who, title, body="some words here", **kw):  # kw: scope, tags
    req = RememberRequest(
        title=title, body=body, scope=kw.get("scope"), tags=tuple(kw.get("tags", ()))
    )
    return (await stack.knowledge.remember(who, req)).item


async def get(stack, item_id):
    return await stack.backend.items.get_system("org1", item_id)


# ------------------------------------------------------------------ retag
async def test_retag_changes_many_and_reports_each_outcome(stack):
    a = await note(stack, ALICE, "a", tags=["old"])
    b = await note(stack, ALICE, "b", tags=["old", "keep"])
    c = await note(stack, ALICE, "c", tags=["new"])
    theirs = await note(stack, CAROL, "carols")  # alice cannot see it
    out = await stack.curation.retag(ALICE, [a.id, b.id, c.id, theirs.id, "nope"], ["new"], ["old"])
    assert sorted(out.changed) == sorted([a.id, b.id]) and out.unchanged == [c.id]
    assert set(out.failed) == {theirs.id, "nope"}
    assert (await get(stack, a.id)).tags == {"new"} and (await get(stack, b.id)).tags == {
        "new",
        "keep",
    }
    assert (await get(stack, a.id)).revision == 2 and (await get(stack, c.id)).revision == 1


async def test_retag_validates_input_and_respects_edit_rights(stack):
    shared = await note(stack, ALICE, "shared", scope=P1)
    out = await stack.curation.retag(BOB, [shared.id], ["x"], [])  # member, not the author
    assert out.failed and not out.changed
    for ids, add, rem in [
        ([], ["x"], []),
        (["a"], [], []),
        (["a"], ["x"], ["x"]),
        (["a"] * 1, ["Bad Tag!!"], []),
    ]:
        with pytest.raises(ValidationError):
            await stack.curation.retag(ALICE, ids, add, rem)
    with pytest.raises(ValidationError):
        await stack.curation.retag(ALICE, [str(i) for i in range(101)], ["x"], [])


async def test_rename_tag_dry_run_then_apply_skips_what_you_cannot_edit(stack):
    mine = [await note(stack, ALICE, f"m{i}", tags=["Radar-Old"]) for i in range(3)]
    bobs = await note(stack, BOB, "bobs", scope=P1, tags=["radar-old"])
    other = await note(stack, ALICE, "other", tags=["unrelated"])
    dry = await stack.curation.rename_tag(ALICE, "radar-old", "radar", dry_run=True)
    assert len(dry.changed) == 3 and bobs.id in dry.failed
    assert (await get(stack, mine[0].id)).tags == {"radar-old"}  # nothing changed yet
    done = await stack.curation.rename_tag(ALICE, "radar-old", "radar")
    assert sorted(done.changed) == sorted(i.id for i in mine)
    assert [(await get(stack, i.id)).tags for i in mine] == [{"radar"}] * 3
    assert (await get(stack, bobs.id)).tags == {"radar-old"} and (
        await get(stack, other.id)
    ).tags == {"unrelated"}
    with pytest.raises(ValidationError):
        await stack.curation.rename_tag(ALICE, "a", "A")


async def test_rename_tag_also_covers_archived_and_pages_through_many(stack):
    items = [await note(stack, ALICE, f"n{i}", tags=["t-old"]) for i in range(130)]
    await stack.knowledge.update(
        ALICE,
        items[0].id,
        __import__("hive_mcp.domain.item", fromlist=["x"]).ItemPatch(status=Status.ARCHIVED),
        1,
    )
    done = await stack.curation.rename_tag(ALICE, "t-old", "t-new")
    assert len(done.changed) == 130 and not done.failed


# ------------------------------------------------------------------ merge
async def test_merge_creates_one_item_links_and_retires_sources(stack):
    a = await note(stack, ALICE, "Range eq v1", "radar range equation", tags=["rf"])
    b = await note(stack, ALICE, "Range eq v2", "radar range formula", tags=["radar"])
    r = await stack.curation.merge(ALICE, [a.id, b.id], "Radar range equation", "Consolidated text")
    assert (
        r.created and r.item.tags == {"rf", "radar"} and sorted(r.retired) == sorted([a.id, b.id])
    )
    assert r.item.scope.level is ScopeLevel.PRIVATE and r.item.kind is Kind.NOTE
    assert (await get(stack, a.id)).status is Status.SUPERSEDED
    graph = await stack.graph.neighbours(ALICE, r.item.id)
    assert {(e.relation, e.dst) for e in graph.edges} == {
        ("supersedes", a.id),
        ("supersedes", b.id),
    }
    again = await stack.curation.merge(
        ALICE, [a.id, b.id], "Radar range equation", "Consolidated text"
    )
    assert not again.created and again.item.id == r.item.id  # retry is safe
    other = await stack.curation.merge(ALICE, [a.id, b.id], "Radar range equation", "A better text")
    assert other.created and other.item.id != r.item.id  # new text is a new merge


async def test_merge_options_archive_keep_and_explicit_tags(stack):
    a, b = await note(stack, ALICE, "a"), await note(stack, ALICE, "b")
    r = await stack.curation.merge(
        ALICE,
        [a.id, b.id],
        "ab",
        "merged",
        tags=["only-this"],
        retire="archived",
        kind=Kind.DECISION,
    )
    assert r.item.tags == {"only-this"} and r.item.kind is Kind.DECISION
    assert (await get(stack, a.id)).status is Status.ARCHIVED
    c, d = await note(stack, ALICE, "c"), await note(stack, ALICE, "d")
    kept = await stack.curation.merge(ALICE, [c.id, d.id], "cd", "merged", retire="keep")
    assert kept.retired == [] and (await get(stack, c.id)).status is Status.ACTIVE


async def test_merge_needs_edit_rights_on_every_source_and_stores_nothing_otherwise(stack):
    mine, bobs = (
        await note(stack, ALICE, "mine", scope=P1),
        await note(stack, BOB, "bobs", scope=P1),
    )
    with pytest.raises(PermissionDenied):
        await stack.curation.merge(ALICE, [mine.id, bobs.id], "x", "y")
    assert len(await stack.backend.items.list_ids_system("org1", None, 10)) == 2
    for ids in ([mine.id], [mine.id, mine.id], [str(i) for i in range(21)]):
        with pytest.raises(ValidationError):
            await stack.curation.merge(ALICE, ids, "x", "y")


async def test_merge_in_a_shared_project_stays_in_the_project(stack):
    a, b = await note(stack, ALICE, "a", scope=P1), await note(stack, ALICE, "b", scope=P1)
    r = await stack.curation.merge(ALICE, [a.id, b.id], "ab", "merged text")
    assert r.item.scope == P1 and r.item.visibility is Visibility.SCOPE
    # sources in different places: the result stays private rather than guessing
    c = await note(stack, ALICE, "c")
    mixed = await stack.curation.merge(ALICE, [r.item.id, c.id], "abc", "merged again")
    assert mixed.item.scope.level is ScopeLevel.PRIVATE


async def test_merge_does_not_widen_access_for_members_when_sources_are_org_wide(stack):
    admin = Principal("root", "org1", Role.ADMIN)
    org_scope = Scope(ScopeLevel.ORG, "org1")
    a = (
        await stack.knowledge.remember(
            admin, RememberRequest(title="a", body="x y", scope=org_scope)
        )
    ).item
    b = (
        await stack.knowledge.remember(
            admin, RememberRequest(title="b", body="x z", scope=org_scope)
        )
    ).item
    r = await stack.curation.merge(admin, [a.id, b.id], "ab", "merged")
    assert r.item.scope == org_scope  # an admin may publish there directly
    member = Principal("m", "org1", Role.MEMBER)  # not the author, not an editor
    with pytest.raises(PermissionDenied):
        await stack.curation.merge(member, [a.id, b.id], "ab2", "merged")  # cannot edit org items


# ------------------------------------------------------------------ similar
async def test_similar_finds_near_duplicates_and_respects_visibility(stack):
    a = await note(
        stack, ALICE, "Radar range", "the radar range equation relates power and distance"
    )
    dup = await note(
        stack, ALICE, "Radar range copy", "the radar range equation relates power and distance"
    )
    near = await note(stack, ALICE, "Other", "radar range equation power distance antenna gain")
    far = await note(stack, ALICE, "Cake", "chocolate butter flour sugar oven")
    private_other = await note(
        stack, CAROL, "carol radar", "the radar range equation relates power and distance"
    )
    found = await stack.curation.similar(ALICE, a.id, min_score=0.3)
    ids = [s.view.item.id for s in found]
    assert ids[0] == dup.id and found[0].likely_duplicate
    assert near.id in ids and far.id not in ids and a.id not in ids and private_other.id not in ids
    assert found[0].score >= found[-1].score


async def test_similar_ignores_archived_and_validates(stack):
    a = await note(stack, ALICE, "a", "unique pelican words")
    b = await note(stack, ALICE, "b", "unique pelican words")
    from hive_mcp.domain.item import ItemPatch  # noqa: PLC0415

    await stack.knowledge.update(ALICE, b.id, ItemPatch(status=Status.ARCHIVED), 1)
    assert await stack.curation.similar(ALICE, a.id, min_score=0.1) == []
    with pytest.raises(ValidationError):
        await stack.curation.similar(ALICE, a.id, limit=0)
