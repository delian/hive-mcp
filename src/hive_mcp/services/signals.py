"""A small, bounded ranking nudge from notes being confirmed as still true (kb_confirm).

Relevance decides; this only breaks near-ties: a note confirmed in the last 90 days gets +2%. It
used to include votes from kb_rate as well, but a vote belongs to a note, not to a question, and a
replay with simulated voters (docs/LLM.md) showed no gain, so votes are feedback data only. Off
unless HIVE_RANK_SIGNALS is set.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from hive_mcp.services.staleness import CONFIRMED_NS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import KeyValueStore

CONFIRM_BONUS = 0.02
CONFIRM_FRESH = timedelta(days=90)


class RankSignals:
    def __init__(self, kv: KeyValueStore, clock: Clock) -> None:
        self._kv, self._clock = kv, clock

    async def factors(self, item_ids: Sequence[str]) -> dict[str, float]:
        """item id -> multiplier (1.0 when nothing is known)."""
        now = self._clock.now()
        out: dict[str, float] = {}
        for item_id in item_ids:
            confirmed = await self._kv.get(CONFIRMED_NS, item_id)
            fresh = confirmed and now - datetime.fromisoformat(confirmed["at"]) <= CONFIRM_FRESH
            out[item_id] = 1.0 + CONFIRM_BONUS if fresh else 1.0
        return out
