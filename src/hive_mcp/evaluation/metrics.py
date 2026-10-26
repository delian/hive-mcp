"""Ranking metrics. `ranked` is the list of returned ids, best first."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

KS = (1, 3, 5, 10)


def first_hit_rank(ranked: Sequence[str], relevant: frozenset[str]) -> int | None:
    """1-based rank of the first relevant result, or None."""
    for rank, item_id in enumerate(ranked, start=1):
        if item_id in relevant:
            return rank
    return None


@dataclass(frozen=True, slots=True)
class Summary:
    queries: int
    recall: dict[int, float]  # k -> share of queries with a relevant result in the top k
    mrr: float
    returned_nothing: float  # share of queries that got no results at all


def summarise(runs: Sequence[tuple[Sequence[str], frozenset[str]]]) -> Summary:
    """Aggregate over queries that have relevant documents."""
    n = len(runs)
    if n == 0:
        return Summary(0, dict.fromkeys(KS, 0.0), 0.0, 0.0)
    ranks = [first_hit_rank(r, rel) for r, rel in runs]
    recall = {k: sum(1 for x in ranks if x is not None and x <= k) / n for k in KS}
    mrr = sum(1.0 / x for x in ranks if x is not None) / n
    return Summary(n, recall, mrr, sum(1 for r, _ in runs if not r) / n)


def false_positive_rate(returned: Sequence[Sequence[str]]) -> float:
    """Share of negative queries that still got any result (they should get none)."""
    return sum(1 for r in returned if r) / len(returned) if returned else 0.0
