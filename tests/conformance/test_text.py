from hive_mcp.domain.access import Acl, read_filter
from hive_mcp.domain.scope import Visibility
from hive_mcp.ports.indexes import TextDoc
from tests.conformance.factory import ALICE, BOB, CAROL, P1, P2, ZOE

ACL1 = Acl("org1", P1, Visibility.SCOPE)


async def add(backend, item_id, text, acl=ACL1):
    await backend.text.upsert(TextDoc(item_id, acl, text))


async def ids(backend, principal, query, limit=10):
    return [h.item_id for h in await backend.text.search(read_filter(principal), query, limit)]


async def test_rarer_and_more_matching_terms_rank_first(backend):
    await add(backend, "both", "klystron amplifier gain measurement")
    await add(backend, "one", "amplifier basics for beginners")
    await add(backend, "none", "recipe for chocolate cake")
    for i in range(5):
        await add(backend, f"filler{i}", "amplifier amplifier common words")
    assert (await ids(backend, ALICE, "klystron amplifier"))[0] == "both"
    assert "none" not in await ids(backend, ALICE, "klystron amplifier")


async def test_matching_is_case_insensitive_for_every_script(backend):
    await add(backend, "bg", "Радар измерва далечина")
    await add(backend, "de", "Straße und Größe")
    assert await ids(backend, ALICE, "РАДАР") == ["bg"]
    assert await ids(backend, ALICE, "радар") == ["bg"]
    assert await ids(backend, ALICE, "GRÖSSE") == ["de"] or await ids(backend, ALICE, "größe") == [
        "de"
    ]


async def test_results_respect_access(backend):
    await add(backend, "p1doc", "secret telemetry", ACL1)
    await add(backend, "p2doc", "secret telemetry", Acl("org1", P2, Visibility.SCOPE))
    await add(backend, "shared", "secret telemetry", Acl("org1", P2, Visibility.ORG))
    await add(backend, "other_org", "secret telemetry", Acl("org2", P1, Visibility.ORG))
    assert set(await ids(backend, ALICE, "telemetry")) == {"p1doc", "shared"}
    assert set(await ids(backend, CAROL, "telemetry")) == {"p2doc", "shared"}
    assert set(await ids(backend, ZOE, "telemetry")) == {"other_org"}


async def test_update_acl_and_delete(backend):
    await add(backend, "d", "orbit decay")
    assert await ids(backend, BOB, "orbit") == ["d"]
    await backend.text.update_acl(
        "d", Acl("org1", P2, Visibility.SCOPE)
    )  # moved to another project
    assert await ids(backend, BOB, "orbit") == []
    assert await ids(backend, CAROL, "orbit") == ["d"]
    await backend.text.delete("d")
    assert await ids(backend, CAROL, "orbit") == []


async def test_upsert_replaces_and_edge_inputs(backend):
    await add(backend, "d", "alpha")
    await add(backend, "d", "beta")
    assert await ids(backend, ALICE, "alpha") == []
    assert await ids(backend, ALICE, "beta") == ["d"]
    assert await ids(backend, ALICE, "") == []
    assert await ids(backend, ALICE, "!!! ???") == []
    assert await ids(backend, ALICE, "beta", limit=0) == []


async def test_common_words_in_the_query_do_not_match_everything(backend):
    await add(backend, "d0", "the quick fox jumps over the lazy dog")
    await add(backend, "d1", "pulse timing for radar")
    assert await ids(backend, ALICE, "what is the radar for") == [
        "d1"
    ]  # "the"/"for" alone match d0
    assert await ids(backend, ALICE, "the")  # a query of only common words still searches
