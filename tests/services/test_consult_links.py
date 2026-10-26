from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.conformance.factory import ALICE, BOB, CAROL, ROOT

P1 = Scope(ScopeLevel.PROJECT, "p1")
P2 = Scope(ScopeLevel.PROJECT, "p2")


async def note(stack, who, title, body, scope=None, **kw):  # kw: vis
    req = RememberRequest(
        title=title, body=body, scope=scope, visibility=kw.get("vis", Visibility.SCOPE)
    )
    return (await stack.knowledge.remember(who, req)).item


async def consult(stack, who, query="pelican migration", **kw):
    return await stack.consult.consult(who, ConsultRequest(query, **kw))


async def test_results_show_links_both_ways(stack):
    source = await note(stack, ALICE, "Source study", "pelican migration source data")
    finding = await note(stack, ALICE, "My finding", "pelican migration conclusion")
    await stack.graph.link(ALICE, finding.id, source.id, "derived_from")
    res = await consult(stack, ALICE, "pelican migration conclusion")
    top = next(m for m in res.primary if m.item_id == finding.id)
    assert [(lk.relation, lk.direction, lk.item_id, lk.title) for lk in top.links] == [
        ("derived_from", "out", source.id, "Source study")
    ]
    cited = next(
        m
        for m in (await consult(stack, ALICE, "pelican migration source")).primary
        if m.item_id == source.id
    )
    assert [(lk.relation, lk.direction) for lk in cited.links] == [("derived_from", "in")]


async def test_a_superseded_result_comes_with_a_pointer_to_its_replacement(stack):
    old = (
        await stack.knowledge.remember(
            ALICE,
            RememberRequest(title="Old method", body="pelican migration old method", tags=("x",)),
        )
    ).item
    new = await note(stack, ALICE, "New method", "completely different words")
    await stack.graph.link(ALICE, new.id, old.id, "supersedes")
    await stack.knowledge.update(ALICE, old.id, ItemPatch(status=Status.SUPERSEDED), old.revision)
    both = await consult(stack, ALICE, "pelican migration old method", statuses=frozenset(Status))
    assert any(
        n.startswith(f'"Old method" has been superseded by "New method" [{new.id}]')
        for n in both.notes
    )
    assert both.primary[0].links[0].relation == "supersedes"
    assert not any('"New method" has been superseded' in n for n in both.notes)  # only the old one
    only_old = await consult(
        stack,
        ALICE,
        "pelican migration old method",
        statuses=frozenset(Status),
        tags_all=frozenset({"x"}),
    )  # the replacement is not among the results: the note says to go and read it
    assert [m.item_id for m in only_old.primary] == [old.id]
    assert any("(not among these results; read it first)" in n for n in only_old.notes)


async def test_contradictions_between_results_are_flagged_once(stack):
    a = await note(stack, ALICE, "Claim A", "pelican migration happens in spring")
    b = await note(stack, ALICE, "Claim B", "pelican migration happens in autumn")
    await stack.graph.link(ALICE, a.id, b.id, "contradicts")
    res = await consult(stack, ALICE)
    flagged = [n for n in res.notes if "contradict" in n]
    assert len(flagged) == 2  # one per direction of reading, each naming both
    assert all("Claim A" in n and "Claim B" in n for n in flagged)


async def test_links_never_reveal_items_the_caller_cannot_see(stack):
    mine = await note(stack, ALICE, "Shared note", "pelican migration shared", scope=P1)
    secret = await note(stack, ALICE, "Private plan", "private words")
    await stack.graph.link(ALICE, mine.id, secret.id, "derived_from")
    await stack.graph.link(ALICE, secret.id, mine.id, "contradicts")
    for who in (BOB, ALICE):
        res = await consult(stack, who)
        match = next(m for m in res.primary if m.item_id == mine.id)
        titles = [lk.title for lk in match.links]
        assert ("Private plan" in titles) == (who is ALICE)
        assert ("Private plan" in " ".join(res.notes)) == (who is ALICE)


async def test_other_groups_items_in_links_are_marked_external(stack):
    theirs = await note(stack, ROOT, "Their study", "unrelated text", scope=P2, vis=Visibility.ORG)
    mine = await note(stack, ALICE, "My follow up", "pelican migration follow up")
    await stack.graph.link(ALICE, mine.id, theirs.id, "derived_from")
    res = await consult(stack, ALICE)
    (link,) = next(m for m in res.primary if m.item_id == mine.id).links
    assert link.external and link.title == "Their study"
    assert not (await consult(stack, CAROL)).primary  # carol cannot see alice's private note at all


async def test_archived_neighbours_and_the_option_to_switch_links_off(stack):
    a = await note(stack, ALICE, "Alpha", "pelican migration alpha")
    b = await note(stack, ALICE, "Beta", "other words entirely")
    await stack.graph.link(ALICE, a.id, b.id, "related")
    assert len(next(m for m in (await consult(stack, ALICE)).primary).links) == 1
    assert next(m for m in (await consult(stack, ALICE, with_links=False)).primary).links == ()
    await stack.knowledge.update(ALICE, b.id, ItemPatch(status=Status.ARCHIVED), b.revision)
    assert next(m for m in (await consult(stack, ALICE)).primary).links == ()


async def test_links_are_capped_and_most_important_relations_kept(stack):
    hub = await note(stack, ALICE, "Hub", "pelican migration hub")
    for i in range(8):
        other = await note(stack, ALICE, f"Other {i}", f"filler text {i}")
        await stack.graph.link(ALICE, hub.id, other.id, "related" if i else "supersedes")
    match = next(m for m in (await consult(stack, ALICE)).primary if m.item_id == hub.id)
    assert len(match.links) == 5 and match.links[0].relation == "supersedes"
