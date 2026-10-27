from datetime import timedelta

import pytest

from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.domain.clock import FixedClock
from hive_mcp.services.signals import CONFIRM_BONUS, CONFIRM_FRESH, CONFIRMED_NS, RankSignals


@pytest.fixture
def parts():
    clock = FixedClock()
    return MemoryKv(clock), clock


async def test_nothing_known_means_no_nudge(parts):
    kv, clock = parts
    assert await RankSignals(kv, clock).factors(["a", "b"]) == {"a": 1.0, "b": 1.0}
    assert await RankSignals(kv, clock).factors([]) == {}


async def test_a_confirmation_lasts_exactly_its_window(parts):
    kv, clock = parts
    signals = RankSignals(kv, clock)
    await kv.put(CONFIRMED_NS, "a", {"at": clock.now().isoformat()})
    assert (await signals.factors(["a", "b"])) == {"a": pytest.approx(1 + CONFIRM_BONUS), "b": 1.0}
    clock.advance(seconds=CONFIRM_FRESH.total_seconds())  # exactly at the edge: still fresh
    assert (await signals.factors(["a"]))["a"] == pytest.approx(1 + CONFIRM_BONUS)
    clock.advance(seconds=1)
    assert (await signals.factors(["a"]))["a"] == 1.0
    assert timedelta(days=90) == CONFIRM_FRESH and CONFIRM_BONUS == 0.02
