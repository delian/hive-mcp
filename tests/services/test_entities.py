import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.conformance.factory import ALICE, BOB, ROOT

P1 = Scope(ScopeLevel.PROJECT, "p1")
P2 = Scope(ScopeLevel.PROJECT, "p2")


async def test_a_new_entity_is_found_by_name_alias_and_spelling_variants(stack):
    r = await stack.entities.save(
        ALICE, "Pulse Doppler Radar", "system", "tracks velocity", ["PD radar", "PDR"]
    )
    assert r.created and r.view.item.kind is Kind.ENTITY and r.view.item.body == "tracks velocity"
    assert {
        "entity/system",
        "alias/pulse-doppler-radar",
        "alias/pd-radar",
        "alias/pdr",
    } <= r.view.item.tags
    for query in ("pulse-doppler radar", "PDR", "pd_radar", "  Pulse   Doppler Radar "):
        found = await stack.entities.resolve(ALICE, query)
        assert [(m.view.item.id, m.how) for m in found] == [(r.view.item.id, "exact")]
    assert await stack.entities.resolve(ALICE, "pdr", "person") == []  # wrong type
    assert await stack.entities.resolve(ALICE, "totally unrelated thing") == []
    assert await stack.entities.resolve(BOB, "pdr") == []  # private to alice


async def test_saving_again_merges_aliases_instead_of_duplicating(stack):
    first = await stack.entities.save(ALICE, "Pulse Doppler Radar", "system", None, ["PDR"])
    assert first.view.item.body == "system: Pulse Doppler Radar"  # default description
    again = await stack.entities.save(
        ALICE, "pdr", "system", "A radar that measures velocity.", ["PD-radar"]
    )
    assert not again.created and again.view.item.id == first.view.item.id
    assert (
        again.added_aliases == ["pd-radar"]
        and again.view.item.body == "A radar that measures velocity."
    )
    assert again.view.item.revision == first.view.item.revision + 1
    same = await stack.entities.save(ALICE, "PDR", "system", "A radar that measures velocity.")
    assert (
        not same.created
        and same.added_aliases == []
        and same.view.item.revision == again.view.item.revision
    )
    ents = await stack.backend.items.list(
        ALICE,
        __import__("hive_mcp.domain.item", fromlist=["x"]).ItemQuery(
            kinds=frozenset({Kind.ENTITY})
        ),
    )
    assert len(ents.items) == 1


async def test_an_alias_owned_by_another_entity_is_not_stolen(stack):
    a = await stack.entities.save(ALICE, "Alpha Unit", "system", None, ["AU"])
    b = await stack.entities.save(
        ALICE, "Beta Unit", "system", None, ["BU", "AU"]
    )  # AU already means Alpha
    assert b.created and b.view.item.id != a.view.item.id and b.conflicts == {"au": a.view.item.id}
    assert "alias/au" not in b.view.item.tags and "alias/bu" in b.view.item.tags
    assert [m.view.item.id for m in await stack.entities.resolve(ALICE, "au")] == [a.view.item.id]
    c = await stack.entities.save(
        ALICE, "Beta Unit", "system", None, ["AU", "B2"]
    )  # merge into Beta
    assert not c.created and c.conflicts == {"au": a.view.item.id} and c.added_aliases == ["b2"]


async def test_close_spellings_are_offered_as_similar(stack):
    existing = await stack.entities.save(ALICE, "Klystron Amplifier", "component")
    found = await stack.entities.resolve(ALICE, "Klystron Amplifer")  # typo
    assert [(m.view.item.id, m.how) for m in found] == [(existing.view.item.id, "similar")]
    dup = await stack.entities.save(ALICE, "Klystron Amplifer", "component")  # created, but warned
    assert dup.created and [m.view.item.id for m in dup.similar] == [existing.view.item.id]


async def test_other_groups_entities_are_visible_but_never_merged_into(stack):
    theirs = await stack.entities.save(ROOT, "Shared Gateway", "system", "org wide", scope=P2)
    await stack.knowledge.move(
        ROOT, theirs.view.item.id, P2, Visibility.ORG, theirs.view.item.revision
    )
    seen = await stack.entities.resolve(ALICE, "shared gateway")
    assert [m.view.tier.value for m in seen] == ["referenced"]
    mine = await stack.entities.save(ALICE, "Shared Gateway", "system", "alice's own view")
    assert (
        not mine.created or mine.view.item.id != theirs.view.item.id
    )  # never edits someone else's
    both = await stack.entities.resolve(ALICE, "shared gateway")
    assert next(m.view.tier.value for m in both) == "primary"  # her own entity sorts first
    assert len(both) == 2


async def test_cannot_create_a_competing_entity_for_a_name_you_cannot_edit(stack):
    boss = await stack.entities.save(BOB, "Team Wiki", "system", scope=P1)
    assert boss.created
    with pytest.raises(ValidationError, match="cannot edit"):
        await stack.entities.save(CAROL_IN_P1, "Team Wiki", "system", scope=P1)


CAROL_IN_P1 = __import__("hive_mcp.domain.principal", fromlist=["x"]).Principal(
    "cat",
    "org1",
    __import__("hive_mcp.domain.principal", fromlist=["x"]).Role.MEMBER,
    projects=frozenset({"p1"}),
)


async def test_mentions_link_notes_to_entities_by_id_or_name(stack):
    ent = await stack.entities.save(ALICE, "Pulse Doppler Radar", "system", None, ["PDR"])
    note = (
        await stack.knowledge.remember(
            ALICE, RememberRequest(title="Test run", body="ran the PDR today")
        )
    ).item
    out = await stack.entities.mention(ALICE, note.id, ["pdr", ent.view.item.id, "ghost", note.id])
    assert out["pdr"] == "linked" and out[ent.view.item.id] == "already linked"
    assert out["ghost"] == "not found" and out[note.id] == "not found"  # a note is not an entity
    graph = await stack.graph.neighbours(ALICE, ent.view.item.id, relations=frozenset({"mentions"}))
    assert {n.view.item.id for n in graph.nodes} == {note.id, ent.view.item.id}


async def test_ingest_links_documents_to_known_entities_in_their_text(stack):
    pdr = await stack.entities.save(ALICE, "Pulse Doppler Radar", "system", None, ["PDR"])
    cake = await stack.entities.save(ALICE, "Cake", "concept")
    result = await stack.ingest.ingest(
        ALICE, "report.md", b"We flew the pulse doppler radar and the PDR logged tracks."
    )
    graph = await stack.graph.neighbours(
        ALICE, result.items[0].id, relations=frozenset({"mentions"})
    )
    linked = {n.view.item.id for n in graph.nodes} - {result.items[0].id}
    assert linked == {pdr.view.item.id}  # 'cake' is not in the text
    off = await stack.ingest.ingest(ALICE, "other.md", b"the pdr again", link_entities=False)
    assert (await stack.graph.neighbours(ALICE, off.items[0].id)).edges == []
    assert cake


async def test_detection_ignores_tiny_aliases_and_other_peoples_private_entities(stack):
    await stack.entities.save(
        ALICE, "Eta", "concept", None, ["E"]
    )  # 1-letter alias would match everywhere
    await stack.entities.save(BOB, "Secret Project", "concept")
    mine = await stack.entities.detect_mentions(ALICE, "e is for everything; secret project")
    assert mine == []
    assert await stack.entities.detect_mentions(ALICE, "about eta") != []


async def test_entities_are_searchable_like_any_note(stack):
    await stack.entities.save(
        ALICE, "Heron Colony", "concept", "A breeding colony of herons on the estuary."
    )
    res = await stack.consult.consult(ALICE, ConsultRequest("breeding colony herons"))
    assert res.primary[0].title == "Heron Colony" and res.primary[0].kind is Kind.ENTITY


async def test_unrelated_names_sharing_a_word_are_not_similar(stack):
    await stack.entities.save(ALICE, "Heron Colony", "concept")
    assert await stack.entities.resolve(ALICE, "Pelican Colony") == []
    assert (await stack.entities.save(ALICE, "Pelican Colony", "concept")).similar == []


async def test_auto_linking_only_uses_your_own_entities_not_other_groups(stack):
    theirs = await stack.entities.save(ROOT, "Shared Gateway", "system", scope=P2)
    await stack.knowledge.move(
        ROOT, theirs.view.item.id, P2, Visibility.ORG, theirs.view.item.revision
    )
    assert (
        await stack.entities.detect_mentions(ALICE, "traffic goes through the shared gateway") == []
    )
