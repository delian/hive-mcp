"""An exact id that only the keyword index can find must not sink below a crowd of vector hits."""

import numpy as np

from hive_mcp.search.ranking import RankingConfig
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.conformance.factory import ALICE
from tests.services.conftest import build_stack


class TopicEmbedder:
    """Two topics: text that mentions "outage" or not. Ids mean nothing to it, like a real model."""

    model_id, dim, min_similarity = "topic-2", 2, 0.5

    @staticmethod
    def _vec(text):
        return np.array([1.0, 0.0] if "outage" in text.lower() else [0.0, 1.0], dtype=np.float32)

    async def embed(self, texts):
        return [self._vec(t) for t in texts]

    async def embed_query(self, text):
        return self._vec(text)


async def first_title(backend, ratio, query="ops 48213 outage"):
    stack = build_stack(backend, TopicEmbedder())
    if not await backend.maintenance.count_system():
        for n in range(6):
            await stack.knowledge.remember(
                ALICE, RememberRequest(title=f"outage runbook {n}", body="what to do in an outage")
            )
        await stack.knowledge.remember(
            ALICE,
            RememberRequest(title="billing hiccup", body="ticket OPS-48213 fixed by a restart"),
        )
        await stack.knowledge.remember(
            ALICE, RememberRequest(title="ops handbook", body="the ops rota and escalation")
        )
    service = ConsultService(
        items=backend.items, text=backend.text, vectors=backend.vectors,
        embedder=stack.embedder, clock=backend.clock,
        ranking=RankingConfig(keyword_lead_ratio=ratio),
    )  # fmt: skip
    res = await service.consult(ALICE, ConsultRequest(query, limit=10))
    return [m.title for m in res.primary]


async def test_exact_id_sinks_below_vector_hits_by_default(backend):
    titles = await first_title(backend, 0.0)
    assert titles.index("billing hiccup") >= 6  # behind every outage note: the known weakness


async def test_opt_in_lead_lifts_the_clearly_best_keyword_hit_to_the_top(backend):
    assert (await first_title(backend, 1.2))[0] == "billing hiccup"


async def test_lead_is_left_alone_when_the_query_is_not_lexical(backend):
    titles = await first_title(backend, 1.2, query="outage")  # vector and keyword agree
    assert titles[0].startswith("outage runbook") and "billing hiccup" not in titles
