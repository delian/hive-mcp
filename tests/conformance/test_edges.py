import asyncio

from hive_mcp.domain.edge import Edge


def edge(backend, src="a", dst="b", rel="related", **kw):  # kw: org, who
    return Edge(kw.get("org", "org1"), src, dst, rel, kw.get("who", "alice"), backend.clock.now())


async def test_add_is_idempotent_and_keeps_the_first_author(backend):
    assert await backend.edges.add(edge(backend, who="alice"))
    assert not await backend.edges.add(edge(backend, who="bob"))
    stored = await backend.edges.get("org1", "a", "b", "related")
    assert stored and stored.author_id == "alice"
    assert await backend.edges.get("org2", "a", "b", "related") is None
    assert await backend.edges.get("org1", "b", "a", "related") is None  # directed


async def test_concurrent_identical_adds_create_one(backend):
    results = await asyncio.gather(*(backend.edges.add(edge(backend)) for _ in range(8)))
    assert sum(results) == 1


async def test_remove(backend):
    await backend.edges.add(edge(backend))
    assert not await backend.edges.remove("org2", "a", "b", "related")
    assert await backend.edges.remove("org1", "a", "b", "related")
    assert not await backend.edges.remove("org1", "a", "b", "related")


async def test_incident_covers_both_directions_filters_relations_and_orgs(backend):
    for e in (edge(backend, "a", "b"), edge(backend, "c", "a", "part_of"), edge(backend, "x", "y")):
        await backend.edges.add(e)
        backend.clock.advance(seconds=1)
    await backend.edges.add(edge(backend, "a", "z", org="org2"))
    got = await backend.edges.incident("org1", ["a"])
    assert [(e.src, e.dst) for e in got] == [("a", "b"), ("c", "a")]
    only = await backend.edges.incident("org1", ["a"], relations=frozenset({"part_of"}))
    assert [e.relation for e in only] == ["part_of"]
    assert len(await backend.edges.incident("org1", ["a", "x"])) == 3
    assert len(await backend.edges.incident("org1", ["a"], limit=1)) == 1
    assert await backend.edges.incident("org1", []) == []
    assert [e.dst for e in await backend.edges.incident("org2", ["a"])] == ["z"]
