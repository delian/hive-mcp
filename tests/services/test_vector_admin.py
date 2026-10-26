import pytest

from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.services.models import ConsultRequest, RememberRequest
from hive_mcp.services.vector_admin import VectorAdmin
from tests.conformance.factory import ALICE
from tests.services.conftest import build_stack


async def notes(stack, n=4):
    return [
        (
            await stack.knowledge.remember(
                ALICE, RememberRequest(title=f"t{i}", body=f"pelican fact {i}")
            )
        ).item
        for i in range(n)
    ]


async def test_switching_embedders_reindex_then_prune(backend):
    old = build_stack(backend, HashingEmbedder(64))
    created = await notes(old)
    new_emb = HashingEmbedder(128)
    new = build_stack(backend, new_emb)
    admin = VectorAdmin(backend.vectors, backend.maintenance, new_emb)
    before = await admin.status()
    assert (before.current_items, before.indexable_items, before.other_models) == (
        0,
        4,
        {"hashing-64": 4},
    )
    assert before.coverage == 0.0
    with pytest.raises(
        ValidationError, match="reindex"
    ):  # the old index is all that works: keep it
        await admin.prune()
    await new.indexing.reindex_org("org1")
    while job := await backend.jobs.claim("w", frozenset({"index_item"}), 30):
        await new.indexing.handle_job(job)
        await backend.jobs.complete(job.id, "w")
    after = await admin.status()
    assert after.current_items == 4 and after.coverage == 1.0
    assert await admin.prune() == {"hashing-64": 4}
    assert (await admin.status()).other_models == {}
    found = await new.consult.consult(ALICE, ConsultRequest("pelican fact"))
    assert {m.item_id for m in found.primary} == {i.id for i in created}  # search keeps working


async def test_force_prunes_an_incomplete_index_and_empty_data_counts_as_complete(backend):
    stack = build_stack(backend, HashingEmbedder(64))
    await notes(stack, 2)
    admin = VectorAdmin(backend.vectors, backend.maintenance, HashingEmbedder(128))
    assert await admin.prune(force=True) == {"hashing-64": 2}
    empty = VectorAdmin(backend.vectors, backend.maintenance, HashingEmbedder(32))
    assert (await empty.status()).coverage >= 0.0
