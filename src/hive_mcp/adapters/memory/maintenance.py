"""In-memory ItemMaintenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hive_mcp.adapters.memory.items import MemoryItems
    from hive_mcp.domain.item import Item
    from hive_mcp.ports.maintenance import ItemFilter


class MemoryMaintenance:
    def __init__(self, items: MemoryItems) -> None:
        self._store = items

    async def purge_system(self, org_id: str, item_id: str) -> Item | None:
        item = self._store._items.get(item_id)
        if item is None or item.org_id != org_id:
            return None
        del self._store._items[item_id]
        stale = [k for k, (_, target) in self._store._idem.items() if target == item_id]
        for key in stale:
            del self._store._idem[key]
        return item

    async def find_system(
        self, flt: ItemFilter, *, after_id: str | None = None, limit: int = 100
    ) -> list[Item]:
        rows = [
            i
            for i in self._store._items.values()
            if (flt.org_id is None or i.org_id == flt.org_id)
            and (flt.author_id is None or i.author_id == flt.author_id)
            and (not flt.kinds or i.kind in flt.kinds)
            and (flt.created_before is None or i.created_at < flt.created_before)
            and (not flt.private_only or i.scope.level.name == "PRIVATE")
            and (after_id is None or i.id > after_id)
        ]
        return sorted(rows, key=lambda i: i.id)[:limit]

    async def count_system(
        self, org_id: str | None = None, *, include_archived: bool = False
    ) -> int:
        return sum(
            1
            for i in self._store._items.values()
            if (org_id is None or i.org_id == org_id)
            and (include_archived or i.status.value != "archived")
        )

    async def count_by_source(self, org_id: str, source: str) -> int:
        return sum(
            1 for i in self._store._items.values() if i.org_id == org_id and i.source == source
        )
