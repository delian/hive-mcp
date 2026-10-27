"""Measuring the curation helpers: contradiction checks, merge proposals and ranking nudges.

Contradictions and merges are scored on labelled sets (`evals/conflicts.json`, `evals/merges.json`)
against the cheap heuristic and, when a model is configured, against the model too. Ranking nudges
are replayed: a simulated crowd votes on what the baseline returned, with a chosen accuracy, and the
same questions are asked again. Everything here is a measurement on the set you give it: write
sets from your own notes before trusting a number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.adapters.memory.backend import MemoryBackend  # noqa: TC001
from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Item
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.evaluation.metrics import summarise
from hive_mcp.evaluation.runner import OWNER, build
from hive_mcp.llm.providers import LlmError
from hive_mcp.services.assist import heuristic_reasons, judge_pair, merge_notes
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.models import ConsultRequest
from hive_mcp.services.signals import RankSignals

if TYPE_CHECKING:
    from hive_mcp.evaluation.dataset import Dataset
    from hive_mcp.llm.providers import LlmProvider
    from hive_mcp.ports.embeddings import Embedder


def _item(title: str, body: str, ident: str) -> Item:
    now = SystemClock().now()
    return Item(
        id=ident, org_id="eval", kind="note", title=title, body=body,  # type: ignore[arg-type]
        scope=Scope(ScopeLevel.PROJECT, "eval"), author_id="eval", created_at=now, updated_at=now,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Scores:
    name: str
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def _score(name: str, verdicts: list[tuple[bool, bool]]) -> Scores:
    """verdicts: (said conflict, really conflict)."""
    tp = sum(s and t for s, t in verdicts)
    fp = sum(s and not t for s, t in verdicts)
    fn = sum(not s and t for s, t in verdicts)
    return Scores(name, tp, fp, fn, len(verdicts) - tp - fp - fn)


async def evaluate_conflicts(raw: object, llm: LlmProvider | None) -> list[Scores]:
    """The heuristic alone, the model alone, and the pipeline as shipped (model, else heuristic)."""
    pairs = raw.get("pairs") if isinstance(raw, dict) else None
    if not isinstance(pairs, list) or not pairs:
        raise ValidationError("a conflict set needs a non-empty 'pairs' list")
    heuristic, model, combined = [], [], []
    for n, pair in enumerate(pairs):
        a = _item(pair["a"]["title"], pair["a"]["body"], f"a{n}")
        b = _item(pair["b"]["title"], pair["b"]["body"], f"b{n}")
        truth = bool(pair["contradict"])
        flagged = bool(heuristic_reasons(a, b))
        heuristic.append((flagged, truth))
        if llm is not None:
            verdict, _ = await judge_pair(llm, a, b)
            model.append((bool(verdict), truth))
            combined.append((verdict if verdict is not None else flagged, truth))
    out = [_score("heuristic", heuristic)]
    if llm is not None:
        out += [_score("model", model), _score("model, else heuristic", combined)]
    return out


@dataclass(frozen=True, slots=True)
class MergeScore:
    group: int
    kept: int
    total: int
    missing: list[str]
    error: str = ""


async def evaluate_merges(raw: object, llm: LlmProvider) -> list[MergeScore]:
    """How many of each group's required facts survive in the proposed note."""
    groups = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValidationError("a merge set needs a non-empty 'groups' list")
    out = []
    for n, group in enumerate(groups):
        items = [_item(x["title"], x["body"], f"m{n}-{i}") for i, x in enumerate(group["notes"])]
        facts = [f.lower() for f in group["facts"]]
        try:
            title, body, _ = await merge_notes(llm, items)
        except LlmError as exc:
            out.append(MergeScore(n, 0, len(facts), facts, str(exc)))
            continue
        text = f"{title}\n{body}".lower()
        missing = [f for f in facts if f not in text]
        out.append(MergeScore(n, len(facts) - len(missing), len(facts), missing))
    return out


@dataclass(frozen=True, slots=True)
class ReplayPoint:
    label: str
    mrr: float
    recall_at_1: float


async def _ranked(
    dataset: Dataset, backend: MemoryBackend, embedder: Embedder, signals: RankSignals | None
) -> list[list[str]]:
    service = ConsultService(
        items=backend.items, text=backend.text, vectors=backend.vectors, embedder=embedder,
        clock=backend.clock, signals=signals,
    )  # fmt: skip
    out = []
    for q in dataset.queries:
        res = await service.consult(OWNER, ConsultRequest(q.text, limit=10))
        out.append([m.item_id for m in res.primary])
    return out


def _point(label: str, dataset: Dataset, ranked: list[list[str]]) -> ReplayPoint:
    pos = [(r, q.relevant) for r, q in zip(ranked, dataset.queries, strict=True) if q.relevant]
    s = summarise(pos)
    return ReplayPoint(label, s.mrr, s.recall[1])


async def replay_votes(
    dataset: Dataset,
    embedder: Embedder,
    *,
    accuracies: tuple[float, ...] = (1.0, 0.8, 0.6, 0.5),
    voters: int = 5,
) -> list[ReplayPoint]:
    """Baseline, then after `voters` simulated people voted on each query's top 3 results.

    A voter is right with probability `accuracy` (useful for a relevant note, not useful for an
    irrelevant one), the same draws every run. The last row is an attack: 40 voters all call one
    unrelated note useful."""
    backend, _ = await build(dataset, embedder)
    baseline = await _ranked(dataset, backend, embedder, None)
    points = [_point("no nudges", dataset, baseline)]
    for accuracy in accuracies:
        rng, crowd = random.Random(7), _signals(backend)  # noqa: S311 - a fixed simulation
        for q, ranked in zip(dataset.queries, baseline, strict=True):
            for item_id in ranked[:3]:
                truth = item_id in q.relevant
                for v in range(voters):
                    says = truth if rng.random() < accuracy else not truth
                    await crowd.record_vote("eval", f"{accuracy}-{v}", item_id, says)
        ranked_after = await _ranked(dataset, backend, embedder, crowd)
        points.append(_point(f"votes {accuracy:.0%} right", dataset, ranked_after))
    wrong = next(
        (
            i
            for q, r in zip(dataset.queries, baseline, strict=True)
            if q.relevant
            for i in r
            if i not in q.relevant
        ),
        None,
    )
    if wrong:
        attack = _signals(backend)
        for v in range(40):
            await attack.record_vote("eval", f"spam{v}", wrong, True)
        ranked_after = await _ranked(dataset, backend, embedder, attack)
        points.append(_point("40 votes for one wrong note", dataset, ranked_after))
    return points


def _signals(backend: MemoryBackend) -> RankSignals:
    return RankSignals(MemoryKv(backend.clock), backend.clock)  # counts start from nothing
