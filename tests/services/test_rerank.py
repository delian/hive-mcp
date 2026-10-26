from hive_mcp.domain.errors import EmbeddingUnavailable
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.conformance.factory import ALICE
from tests.services.conftest import build_stack


class ScriptedReranker:
    """Scores passages by a keyword table; records what it was asked."""

    model_id = "fake"

    def __init__(self, scores, min_score=-100.0, fail=False):
        self.scores, self.min_score, self.fail, self.calls = scores, min_score, fail, []

    async def score(self, query, passages):
        self.calls.append(list(passages))
        if self.fail:
            raise EmbeddingUnavailable("model gone")
        return [next((v for k, v in self.scores.items() if k in p), 0.0) for p in passages]


async def setup(backend, reranker, depth=15):
    """Four notes (created once per backend) and a ConsultService with the given reranker."""
    stack = build_stack(backend)
    if not await backend.maintenance.count_system():
        for title in ("alpha", "bravo", "charlie", "delta"):
            await stack.knowledge.remember(
                ALICE, RememberRequest(title=title, body=f"pelican migration notes {title}")
            )
    service = ConsultService(
        items=backend.items, text=backend.text, vectors=backend.vectors,
        embedder=stack.embedder, clock=backend.clock, reranker=reranker, rerank_depth=depth,
    )  # fmt: skip
    return stack, service


async def titles(service, **kw):
    res = await service.consult(ALICE, ConsultRequest("pelican migration", **kw))
    return [m.title for m in res.primary], res


async def test_reranker_decides_the_order_and_sees_title_and_text(backend):
    rr = ScriptedReranker({"charlie": 9.0, "alpha": 3.0, "delta": -2.0})
    _, service = await setup(backend, rr)
    order, res = await titles(service)
    assert order == ["charlie", "alpha", "bravo", "delta"]  # bravo scored 0 -> between 3 and -2
    assert all(m.score <= 1.0 for m in res.primary)  # scores are now probabilities
    assert all(
        p.startswith(("alpha", "bravo", "charlie", "delta")) for p in rr.calls[0]
    )  # title first
    assert "pelican migration notes" in rr.calls[0][0]


async def test_equal_scores_keep_the_first_stage_order(backend):
    _, plain = await setup(backend, None)
    baseline, _ = await titles(plain)
    _, service = await setup(backend, ScriptedReranker({}))
    assert (await titles(service))[0] == baseline


async def test_candidates_below_min_score_are_dropped_and_depth_limits_the_work(backend):
    _, service = await setup(
        backend, ScriptedReranker({"alpha": 5.0, "bravo": -9.0, "delta": -9.0}, min_score=-4.0)
    )
    assert (await titles(service))[0] == ["alpha", "charlie"]
    rr = ScriptedReranker({})
    _, shallow = await setup(backend, rr, depth=2)
    await titles(shallow, limit=1)
    assert len(rr.calls[-1]) == 2  # only the best two candidates were scored
    await titles(shallow, limit=3)
    assert (
        len(rr.calls[-1]) == 4
    )  # asking for more results widens the window (limit * 2, capped by what exists)


async def test_an_unavailable_reranker_degrades_to_first_stage_with_a_note(backend):
    _, service = await setup(backend, ScriptedReranker({}, fail=True))
    order, res = await titles(service)
    assert len(order) == 4 and any("reranking unavailable" in n for n in res.notes)


async def test_extreme_scores_do_not_overflow(backend):
    _, service = await setup(
        backend, ScriptedReranker({"alpha": 5000.0, "bravo": -5000.0}, min_score=-1e9)
    )
    order, res = await titles(service)
    assert order[0] == "alpha" and order[-1] == "bravo" and 0.0 <= res.primary[-1].score <= 1.0
