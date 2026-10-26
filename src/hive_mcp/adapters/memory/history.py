"""In-memory item history (versions are kept by MemoryItems as it applies changes)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import NotFound
from hive_mcp.domain.history import KEEP_VERSIONS, current_version, readable

if TYPE_CHECKING:
    from hive_mcp.adapters.memory.items import MemoryItems
    from hive_mcp.domain.history import ItemVersion
    from hive_mcp.domain.principal import Principal


class MemoryHistory:
    def __init__(self, items: MemoryItems) -> None:
        self._store = items

    async def versions(
        self, principal: Principal, item_id: str, limit: int = 50
    ) -> list[ItemVersion]:
        view = await self._store.get(principal, item_id)  # NotFound when invisible
        old = reversed(self._store.versions.get(item_id, []))
        flt = access.read_filter(principal)
        return readable(flt, [current_version(view.item), *old])[:limit]

    async def version(self, principal: Principal, item_id: str, revision: int) -> ItemVersion:
        for v in await self.versions(principal, item_id, limit=KEEP_VERSIONS + 1):
            if v.revision == revision:
                return v
        raise NotFound(f"{item_id}@{revision}")
