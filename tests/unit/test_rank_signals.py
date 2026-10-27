import math
from datetime import timedelta

import pytest

from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.services.signals import (
    CONFIRM_BONUS,
    CONFIRM_FRESH,
    CONFIRMED_NS,
    SOFTNESS,
    VOTE_RANGE,
    VOTER_NS,
    VOTES_NS,
    RankSignals,
    vote_factor,
)


def test_the_vote_factor_is_the_documented_curve():
    assert vote_factor(4, 1) == pytest.approx(1 + VOTE_RANGE * math.tanh(3 / SOFTNESS))
    assert vote_factor(1, 4) == pytest.approx(1 - VOTE_RANGE * math.tanh(3 / SOFTNESS))
    assert vote_factor(2, 2) == 1.0


@pytest.fixture
def parts():
    clock = FixedClock()
    return MemoryKv(clock), clock


async def test_counts_and_voter_records_follow_every_kind_of_vote(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    await signals.record_vote("o", "a", "n", True)
    await signals.record_vote("o", "b", "n", False)
    assert await kv.get(VOTES_NS, "n") == {"up": 1, "down": 1}
    await signals.record_vote("o", "a", "n", False)  # up -> down
    assert await kv.get(VOTES_NS, "n") == {"up": 0, "down": 2}
    await signals.record_vote("o", "a", "n", True)  # down -> up
    assert await kv.get(VOTES_NS, "n") == {"up": 1, "down": 1}
    await signals.record_vote("o", "a", "n", True)  # unchanged
    assert await kv.get(VOTES_NS, "n") == {"up": 1, "down": 1}


async def test_a_voter_is_recorded_per_org_person_and_note_without_the_plain_names(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    await signals.record_vote("o", "a", "n", True)
    await signals.record_vote("o", "a", "m", False)
    await signals.record_vote("o2", "a", "n", True)
    assert await kv.get(VOTES_NS, "n") == {"up": 2, "down": 0}
    assert await kv.get(VOTES_NS, "m") == {"up": 0, "down": 1}
    keys = [k for ns, k in kv._rows if ns == VOTER_NS]
    assert len(keys) == 3
    assert all(len(k.split(":", 1)[1]) == 24 for k in keys)  # a 24-character hash, no names


async def test_a_counter_never_goes_below_zero_even_for_a_damaged_record(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    await signals.record_vote("o", "a", "n", True)
    await kv.put(VOTES_NS, "n", {"up": 0, "down": 0})  # lost counts
    await signals.record_vote("o", "a", "n", False)
    assert await kv.get(VOTES_NS, "n") == {"up": 0, "down": 1}


async def test_factors_combine_votes_and_a_confirmation_that_lasts_exactly_its_window(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    assert await signals.factors(["a", "b"]) == {"a": 1.0, "b": 1.0}
    await signals.record_vote("o", "p", "a", True)
    await kv.put(CONFIRMED_NS, "a", {"at": clock.now().isoformat()})
    await kv.put(CONFIRMED_NS, "b", {"at": clock.now().isoformat()})
    both = await signals.factors(["a", "b"])
    assert both["a"] == pytest.approx(vote_factor(1, 0) * (1 + CONFIRM_BONUS))
    assert both["b"] == pytest.approx(1 + CONFIRM_BONUS)
    clock.advance(seconds=CONFIRM_FRESH.total_seconds())  # exactly at the edge: still fresh
    assert (await signals.factors(["b"]))["b"] == pytest.approx(1 + CONFIRM_BONUS)
    clock.advance(seconds=1)
    assert (await signals.factors(["b"]))["b"] == 1.0
    assert timedelta(days=90) == CONFIRM_FRESH


async def test_taking_a_vote_back_lowers_the_counter_that_held_it(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    for person in "abc":
        await signals.record_vote("o", person, "n", True)
    await signals.record_vote("o", "a", "n", False)  # one of three ups becomes a down
    assert await kv.get(VOTES_NS, "n") == {"up": 2, "down": 1}
    await signals.record_vote("o", "a", "n", True)
    assert await kv.get(VOTES_NS, "n") == {"up": 3, "down": 0}


async def test_the_stored_vote_is_plus_or_minus_one(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    await signals.record_vote("o", "a", "n", True)
    await signals.record_vote("o", "b", "n", False)
    stored = sorted(row[0]["v"] for (ns, _), row in kv._rows.items() if ns == VOTER_NS)
    assert stored == [-1, 1]
