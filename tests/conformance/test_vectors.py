import numpy as np
import pytest

from hive_mcp.domain.access import Acl, read_filter
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Visibility
from tests.conformance.factory import ALICE, BOB, CAROL, P1, P2, ZOE

ACL1 = Acl("org1", P1, Visibility.SCOPE)
M = "model-a"


def v(*xs):
    return np.array(xs, dtype=np.float32)


async def search(backend, who, q, model=M, limit=10):
    return await backend.vectors.search(read_filter(who), model, q, limit)


async def test_nearest_first_one_hit_per_item_with_best_chunk(backend):
    await backend.vectors.replace("a", M, ACL1, [v(1, 0, 0), v(0, 1, 0), v(0, 0, 1)])
    await backend.vectors.replace("b", M, ACL1, [v(0.9, 0.1, 0)])
    hits = await search(backend, ALICE, v(0, 0.9, 0.1))
    assert [h.item_id for h in hits] == ["a", "b"]
    assert hits[0].chunk_ord == 1 and hits[0].score > hits[1].score


async def test_replace_removes_stale_chunks(backend):
    await backend.vectors.replace("a", M, ACL1, [v(1, 0, 0), v(0, 1, 0), v(0, 0, 1)])
    await backend.vectors.replace("a", M, ACL1, [v(1, 0, 0)])  # re-chunked into one chunk
    hits = await search(backend, ALICE, v(0, 1, 0))
    assert [round(h.score, 3) for h in hits] == [0.0] and hits[0].chunk_ord == 0
    await backend.vectors.replace("a", M, ACL1, [])
    assert await search(backend, ALICE, v(1, 0, 0)) == []


async def test_models_are_isolated_and_dimensions_enforced(backend):
    await backend.vectors.replace("a", M, ACL1, [v(1, 0, 0)])
    await backend.vectors.replace("a", "model-b", ACL1, [v(1, 0, 0, 0)])  # other model, other dim
    assert [h.item_id for h in await search(backend, ALICE, v(1, 0, 0, 0), "model-b")] == ["a"]
    assert await search(backend, ALICE, v(1, 0, 0), "unknown-model") == []
    with pytest.raises(ValidationError):
        await backend.vectors.replace("c", M, ACL1, [v(1, 0)])
    with pytest.raises(ValidationError):
        await search(backend, ALICE, v(1, 0))


@pytest.mark.parametrize("bad", [v(0, 0, 0), v(1, np.nan, 0), v(1, np.inf, 0), v()])
async def test_bad_vectors_are_rejected_without_touching_state(backend, bad):
    await backend.vectors.replace("a", M, ACL1, [v(1, 0, 0)])
    with pytest.raises(ValidationError):
        await backend.vectors.replace("a", M, ACL1, [v(0, 1, 0), bad])  # whole replace must fail
    hits = await search(backend, ALICE, v(1, 0, 0))
    assert [h.item_id for h in hits] == ["a"] and hits[0].score > 0.99


async def test_cosine_ignores_magnitude(backend):
    await backend.vectors.replace("a", M, ACL1, [v(100, 0, 0)])
    assert (await search(backend, ALICE, v(0.001, 0, 0)))[0].score == pytest.approx(1.0)


async def test_results_respect_access_and_acl_updates(backend):
    await backend.vectors.replace("p1", M, ACL1, [v(1, 0, 0)])
    await backend.vectors.replace("p2", M, Acl("org1", P2, Visibility.SCOPE), [v(1, 0, 0)])
    await backend.vectors.replace("pub", M, Acl("org1", P2, Visibility.ORG), [v(1, 0, 0)])
    await backend.vectors.replace("t2", M, Acl("org2", P1, Visibility.ORG), [v(1, 0, 0)])
    q = v(1, 0, 0)
    assert {h.item_id for h in await search(backend, ALICE, q)} == {"p1", "pub"}
    assert {h.item_id for h in await search(backend, CAROL, q)} == {"p2", "pub"}
    assert {h.item_id for h in await search(backend, ZOE, q)} == {"t2"}
    await backend.vectors.update_acl("p1", Acl("org1", P2, Visibility.SCOPE))
    assert {h.item_id for h in await search(backend, BOB, q)} == {"pub"}
    await backend.vectors.delete("pub")
    assert {h.item_id for h in await search(backend, BOB, q)} == set()


async def test_limit(backend):
    for i in range(5):
        await backend.vectors.replace(f"i{i}", M, ACL1, [v(1, i * 0.1, 0)])
    assert len(await search(backend, ALICE, v(1, 0, 0), limit=3)) == 3
    assert await search(backend, ALICE, v(1, 0, 0), limit=0) == []


async def test_one_item_with_many_chunks_cannot_crowd_out_others(backend):
    many = [v(1, 0.001 * i, 0) for i in range(120)]  # 120 near-identical chunks of one item
    await backend.vectors.replace("big", M, ACL1, many)
    await backend.vectors.replace("b", M, ACL1, [v(0.9, 0.3, 0)])
    await backend.vectors.replace("c", M, ACL1, [v(0.8, 0.5, 0)])
    hits = await search(backend, ALICE, v(1, 0, 0), limit=3)
    assert {h.item_id for h in hits} == {"big", "b", "c"}  # deeper chunks must still be reached
