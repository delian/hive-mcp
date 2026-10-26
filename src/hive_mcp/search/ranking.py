"""Rank fusion and relevance weighting: pure functions, weights in `RankingConfig` (tunable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 - used in a runtime-evaluated dataclass annotation

from hive_mcp.domain.access import Tier
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.scope import ScopeLevel

RRF_K = 60


def rrf(
    rankings: list[list[str]], k: int = RRF_K, weights: list[float] | None = None
) -> dict[str, float]:
    """Reciprocal rank fusion: a list adds weight/(k + rank) per item (repeats count once)."""
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights or [1.0] * len(rankings), strict=True):
        seen: set[str] = set()
        for rank, item_id in enumerate(ranking, start=1):
            if item_id in seen:
                continue
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return scores


@dataclass(frozen=True, slots=True)
class RankingConfig:
    # Closer scopes are more relevant. Private notes are the caller's own working context.
    scope_weight: dict[ScopeLevel, float] = field(
        default_factory=lambda: {
            ScopeLevel.PRIVATE: 1.0,
            ScopeLevel.PROJECT: 1.0,
            ScopeLevel.TEAM: 0.85,
            ScopeLevel.ORG: 0.7,
        }
    )
    # Hybrid fusion, tuned on evals/core.json (see docs/EVALUATION.md): a real embedding model is
    # the stronger signal, and keyword hits beyond the first few are mostly noise.
    vector_weight: float = 2.0
    keyword_depth: int = 5
    referenced_weight: float = 0.6  # knowledge from groups the caller does not belong to
    status_weight: dict[Status, float] = field(
        default_factory=lambda: {
            Status.ACTIVE: 1.0,
            Status.DRAFT: 0.8,
            Status.SUPERSEDED: 0.4,
            Status.ARCHIVED: 0.0,
        }
    )
    # Days after which relevance halves; None = does not age (reference material).
    half_life_days: dict[Kind, float | None] = field(
        default_factory=lambda: {
            Kind.SESSION: 30.0,
            Kind.COMPACTION: 45.0,
            Kind.NOTE: 180.0,
            Kind.RESEARCH: 365.0,
            Kind.MEASUREMENT: 365.0,
            Kind.DECISION: 365.0,
            Kind.FORMULA: None,
            Kind.BEST_PRACTICE: None,
            Kind.DOC: None,
            Kind.HOWTO: None,
            Kind.ENTITY: None,
        }
    )


def freshness(kind: Kind, updated_at: datetime, now: datetime, cfg: RankingConfig) -> float:
    half_life = cfg.half_life_days.get(kind)
    if half_life is None:
        return 1.0
    age_days = max((now - updated_at).total_seconds() / 86_400, 0.0)  # clock skew never boosts
    return float(0.5 ** (age_days / half_life))


def weight(  # noqa: PLR0913 - one weight per ranking signal
    cfg: RankingConfig,
    *,
    level: ScopeLevel,
    tier: Tier,
    status: Status,
    kind: Kind,
    updated_at: datetime,
    now: datetime,
) -> float:
    scope = cfg.scope_weight[level] * (cfg.referenced_weight if tier is Tier.REFERENCED else 1.0)
    fresh = freshness(kind, updated_at, now, cfg)
    return float(scope * cfg.status_weight[status] * (0.5 + 0.5 * fresh))


def fuse(text_ids: list[str], vector_ids: list[str], config: RankingConfig) -> dict[str, float]:
    """The hybrid ranking: weighted reciprocal rank fusion of keyword and vector results."""
    return rrf([text_ids[: config.keyword_depth], vector_ids], weights=[1.0, config.vector_weight])
