"""Applies retention rules: purges what is older than a rule allows, in every organisation."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.ports.maintenance import ItemFilter

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.retention import RetentionRule
    from hive_mcp.ports.maintenance import ItemMaintenance
    from hive_mcp.services.purge import PurgeService

SYSTEM = "system:retention"
PAGE = 100
log = logging.getLogger(__name__)


class RetentionService:
    def __init__(
        self,
        maintenance: ItemMaintenance,
        purge: PurgeService,
        rules: list[RetentionRule],
        clock: Clock,
    ) -> None:
        self._maintenance, self._purge, self._rules, self._clock = maintenance, purge, rules, clock

    @property
    def rules(self) -> list[RetentionRule]:
        return list(self._rules)

    async def run(self, *, dry_run: bool = False, max_items: int = 500) -> dict[str, int]:
        """Rule description -> items purged (or that would be). At most `max_items` per run in
        total: a first run on a big base is spread over several sweeps, not one long one."""
        done: dict[str, int] = {}
        budget = max_items
        for rule in self._rules:
            cutoff = self._clock.now() - timedelta(days=rule.days)
            flt = ItemFilter(
                kinds=frozenset({rule.kind}) if rule.kind else frozenset(),
                scope_level=rule.scope_level,
                updated_before=cutoff,
            )
            count, after = 0, None
            while budget > 0:
                page = await self._maintenance.find_system(
                    flt, after_id=after, limit=min(PAGE, budget)
                )
                if not page:
                    break
                after = page[-1].id
                for item in page:
                    if not dry_run:
                        await self._purge.purge_item(item, SYSTEM)
                    count += 1
                    budget -= 1
            done[rule.describe()] = count
            if count and not dry_run:
                log.info("retention rule purged %d: %s", count, rule.describe())
        return done
