"""Applies retention rules: purges or archives what is older than a rule allows, in every org."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import Conflict, NotFound
from hive_mcp.domain.item import Status
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.ports.maintenance import ItemFilter

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.retention import RetentionRule
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.maintenance import ItemMaintenance
    from hive_mcp.services.indexing import IndexService
    from hive_mcp.services.purge import PurgeService

SYSTEM = "system:retention"
PAGE = 100
log = logging.getLogger(__name__)


class RetentionService:
    def __init__(  # noqa: PLR0913, PLR0917 - collaborators
        self,
        maintenance: ItemMaintenance,
        purge: PurgeService,
        rules: list[RetentionRule],
        clock: Clock,
        items: ItemRepository | None = None,
        indexing: IndexService | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._maintenance, self._purge, self._rules, self._clock = maintenance, purge, rules, clock
        self._items, self._indexing, self._audit = items, indexing, audit

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
                exclude_archived=rule.action == "archive",
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
                    if not dry_run and not await self._apply(rule.action, item):
                        continue  # changed by someone in the meantime: next sweep
                    count += 1
                    budget -= 1
            done[rule.describe()] = count
            if count and not dry_run:
                log.info("retention rule applied to %d items: %s", count, rule.describe())
        return done

    async def _apply(self, action: str, item: Item) -> bool:
        if action == "purge":
            if self._items is not None:  # an edit since the page was read restarts the clock
                current = await self._items.get_system(item.org_id, item.id)
                if current is None or current.revision != item.revision:
                    return False
            await self._purge.purge_item(item, SYSTEM)
            return True
        assert self._items and self._indexing and self._audit  # noqa: S101 - wired by bootstrap
        try:
            changed = await self._items.set_status_system(
                item.org_id, item.id, Status.ARCHIVED, item.revision
            )
        except (Conflict, NotFound):
            return False
        await self._indexing.index_or_enqueue(changed)  # archived items leave the search indexes
        await self._audit.append(
            AuditEvent(
                item.org_id, SYSTEM, "item.archive", self._clock.now(), item.id,
                {"kind": str(item.kind), "scope": item.scope.level.name.lower()},
            )
        )  # fmt: skip
        return True
