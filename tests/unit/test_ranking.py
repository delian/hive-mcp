from datetime import UTC, datetime, timedelta

import pytest

from hive_mcp.domain.access import Tier
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.search.ranking import RankingConfig, freshness, rrf, weight

NOW = datetime(2026, 6, 1, tzinfo=UTC)
CFG = RankingConfig()


def w(**kw):
    base = dict(
        level=ScopeLevel.PROJECT,
        tier=Tier.PRIMARY,
        status=Status.ACTIVE,
        kind=Kind.NOTE,
        updated_at=NOW,
        now=NOW,
    )
    return weight(CFG, **{**base, **kw})


def test_rrf_rewards_agreement_between_rankings():
    scores = rrf([["a", "b", "c"], ["c", "a", "d"]])
    assert (
        max(scores, key=scores.get) == "a"
        and scores["a"] > scores["b"]
        and set(scores) == set("abcd")
    )


def test_rrf_counts_a_repeated_id_once_per_list_and_handles_empty():
    assert rrf([["a", "a", "a"]]) == rrf([["a"]]) and rrf([]) == {} and rrf([[], []]) == {}


def test_closer_scopes_and_own_tier_rank_higher():
    assert w(level=ScopeLevel.PROJECT) > w(level=ScopeLevel.TEAM) > w(level=ScopeLevel.ORG)
    assert w(tier=Tier.PRIMARY) > w(tier=Tier.REFERENCED)


def test_status_penalties():
    assert (
        w(status=Status.ACTIVE)
        > w(status=Status.DRAFT)
        > w(status=Status.SUPERSEDED)
        > w(status=Status.ARCHIVED)
        == 0
    )


def test_freshness_halves_per_half_life_and_reference_material_never_ages():
    old = NOW - timedelta(days=30)
    assert freshness(Kind.SESSION, old, NOW, CFG) == pytest.approx(0.5)
    assert freshness(Kind.FORMULA, NOW - timedelta(days=5000), NOW, CFG) == 1.0
    assert w(kind=Kind.SESSION, updated_at=old) < w(kind=Kind.SESSION)


def test_future_timestamps_never_boost():
    assert freshness(Kind.SESSION, NOW + timedelta(days=10), NOW, CFG) == 1.0
