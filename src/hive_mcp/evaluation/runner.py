"""Index an evaluation set in memory and score keyword, vector and hybrid retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.adapters.memory.backend import MemoryBackend
from hive_mcp.domain.access import read_filter
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.item import Item
from hive_mcp.domain.principal import Principal
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.evaluation.metrics import Summary, false_positive_rate, summarise
from hive_mcp.search.ranking import RankingConfig, fuse
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.indexing import IndexService
from hive_mcp.services.models import ConsultRequest

if TYPE_CHECKING:
    from hive_mcp.evaluation.dataset import Dataset
    from hive_mcp.ports.embeddings import Embedder
    from hive_mcp.ports.rerank import Reranker

OWNER = Principal("eval", "eval", projects=frozenset({"eval"}))
POOL = 50


@dataclass(frozen=True, slots=True)
class ModeReport:
    mode: str
    positives: Summary
    false_positive_rate: float  # on negative queries


@dataclass(frozen=True, slots=True)
class FloorPoint:
    floor: float
    recall_at_3: float
    false_positive_rate: float


@dataclass(frozen=True, slots=True)
class Report:
    dataset: str
    embedder: str
    modes: list[ModeReport]
    sweep: list[FloorPoint]


async def build(dataset: Dataset, embedder: Embedder) -> tuple[MemoryBackend, IndexService]:
    backend = MemoryBackend()
    indexing = IndexService(backend.items, backend.text, backend.vectors, embedder, backend.jobs)
    now = SystemClock().now()
    scope = Scope(ScopeLevel.PROJECT, "eval")
    for doc in dataset.docs:
        item = Item(
            id=doc.id, org_id=OWNER.org_id, kind="note", title=doc.title, body=doc.body,  # type: ignore[arg-type]
            scope=scope, author_id=OWNER.id, created_at=now, updated_at=now,
            tags=normalize_tags(doc.tags),
        )  # fmt: skip
        await backend.items.create(OWNER, item)
        await indexing.index_item(item)
    return backend, indexing


async def _rankings(
    dataset: Dataset, backend: MemoryBackend, embedder: Embedder, floor: float
) -> dict[str, list[list[str]]]:
    flt = read_filter(OWNER)
    out: dict[str, list[list[str]]] = {"keyword": [], "vector": [], "hybrid": []}
    for q in dataset.queries:
        text = [h.item_id for h in await backend.text.search(flt, q.text, POOL)]
        vec_hits = await backend.vectors.search(
            flt, embedder.model_id, await embedder.embed_query(q.text), POOL
        )
        vec = [h.item_id for h in vec_hits if h.score >= floor]
        fused = fuse(text, vec, RankingConfig())
        out["keyword"].append(text)
        out["vector"].append(vec)
        out["hybrid"].append(sorted(fused, key=lambda i: -fused[i]))
    return out


def _report(dataset: Dataset, ranked: list[list[str]], mode: str) -> ModeReport:
    pos = [(r, q.relevant) for r, q in zip(ranked, dataset.queries, strict=True) if q.relevant]
    neg = [r for r, q in zip(ranked, dataset.queries, strict=True) if not q.relevant]
    return ModeReport(mode, summarise(pos), false_positive_rate(neg))


async def evaluate(
    dataset: Dataset,
    embedder: Embedder,
    *,
    floor: float | None = None,
    sweep: tuple[float, ...] = (),
) -> Report:
    backend, _ = await build(dataset, embedder)
    use_floor = embedder.min_similarity if floor is None else floor
    ranked = await _rankings(dataset, backend, embedder, use_floor)
    modes = [_report(dataset, ranked[m], m) for m in ("keyword", "vector", "hybrid")]
    points = []
    for f in sweep:
        r = _report(dataset, (await _rankings(dataset, backend, embedder, f))["vector"], "vector")
        points.append(FloorPoint(f, r.positives.recall[3], r.false_positive_rate))
    return Report(dataset.name, embedder.model_id, modes, points)


async def consult_check(
    dataset: Dataset, embedder: Embedder, reranker: Reranker | None = None
) -> ModeReport:
    """The same questions through the real ConsultService (adds filtering, weighting, snippets)."""
    backend, _ = await build(dataset, embedder)
    service = ConsultService(
        items=backend.items, text=backend.text, vectors=backend.vectors,
        embedder=embedder, clock=backend.clock, reranker=reranker,
    )  # fmt: skip
    ranked = []
    for q in dataset.queries:
        res = await service.consult(OWNER, ConsultRequest(q.text, limit=10))
        ranked.append([m.item_id for m in res.primary])
    return _report(dataset, ranked, "rerank" if reranker else "consult")


def format_report(report: Report) -> str:
    lines = [f"dataset {report.dataset}, embedder {report.embedder}", ""]
    lines.append(
        f"{'mode':<9} {'R@1':>5} {'R@3':>5} {'R@5':>5} {'R@10':>5} {'MRR':>5} "
        f"{'empty':>6} {'neg-FP':>7}"
    )
    for m in report.modes:
        r = m.positives.recall
        lines.append(
            f"{m.mode:<9} {r[1]:5.2f} {r[3]:5.2f} {r[5]:5.2f} {r[10]:5.2f} {m.positives.mrr:5.2f} "
            f"{m.positives.returned_nothing:6.2f} {m.false_positive_rate:7.2f}"
        )
    if report.sweep:
        lines += ["", "vector floor sweep:   floor   R@3   neg-FP"]
        lines += [
            f"                     {p.floor:6.2f} {p.recall_at_3:5.2f} {p.false_positive_rate:8.2f}"
            for p in report.sweep
        ]
    return "\n".join(lines)
