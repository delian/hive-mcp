"""Small, bounded ranking nudges from what people said about results (kb_rate) and from notes
being confirmed as still true (kb_confirm).

Relevance decides; these only break near-ties: a result with many "useful" votes can overtake one
that is a little more relevant, never one that is clearly less so (the factor stays within
+/-6% for votes and +2% for a recent confirmation). One vote per person and note, which they can
change; counts only, no query text. Off unless HIVE_RANK_SIGNALS is set.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from hive_mcp.services.staleness import CONFIRMED_NS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import KeyValueStore

VOTES_NS = "rank:votes"
VOTER_NS = "rank:voter"
VOTE_RANGE = 0.06
CONFIRM_BONUS = 0.02
CONFIRM_FRESH = timedelta(days=90)
SOFTNESS = 3.0  # votes needed to reach ~76% of the maximum nudge


def vote_factor(up: int, down: int) -> float:
    return 1.0 + VOTE_RANGE * math.tanh((up - down) / SOFTNESS)


class RankSignals:
    def __init__(self, kv: KeyValueStore, clock: Clock) -> None:
        self._kv, self._clock = kv, clock

    async def record_vote(self, org_id: str, person_id: str, item_id: str, useful: bool) -> None:
        """Count a vote, replacing the same person's earlier vote on the same note."""
        who = hashlib.sha256(f"{org_id}\0{person_id}".encode()).hexdigest()[:24]
        key = f"{item_id}:{who}"
        previous = await self._kv.get(VOTER_NS, key)
        now = 1 if useful else -1
        if previous and previous["v"] == now:
            return
        counts = await self._kv.get(VOTES_NS, item_id) or {"up": 0, "down": 0}
        if previous:  # a changed mind: take the old vote back
            counts["up" if previous["v"] == 1 else "down"] = max(
                counts["up" if previous["v"] == 1 else "down"] - 1, 0
            )
        counts["up" if useful else "down"] += 1
        await self._kv.put(VOTES_NS, item_id, counts)
        await self._kv.put(VOTER_NS, key, {"v": now})

    async def factors(self, item_ids: Sequence[str]) -> dict[str, float]:
        """item id -> multiplier (1.0 when nothing is known)."""
        now = self._clock.now()
        out: dict[str, float] = {}
        for item_id in item_ids:
            factor = 1.0
            if counts := await self._kv.get(VOTES_NS, item_id):
                factor *= vote_factor(int(counts["up"]), int(counts["down"]))
            confirmed = await self._kv.get(CONFIRMED_NS, item_id)
            if confirmed and now - datetime.fromisoformat(confirmed["at"]) <= CONFIRM_FRESH:
                factor *= 1.0 + CONFIRM_BONUS
            out[item_id] = factor
        return out
