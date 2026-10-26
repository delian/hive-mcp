"""Read access to an item's earlier versions. Versions are recorded by the item repository inside
the same transaction as every update or move, so history can never miss a change."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hive_mcp.domain.history import ItemVersion
    from hive_mcp.domain.principal import Principal


class ItemHistory(Protocol):
    async def versions(
        self, principal: Principal, item_id: str, limit: int = 50
    ) -> list[ItemVersion]:
        """Newest first, current version included. NotFound if the item itself is invisible;
        versions
        the caller could not have read at the time are omitted."""

    async def version(self, principal: Principal, item_id: str, revision: int) -> ItemVersion:
        """NotFound for an unknown revision, a dropped one, or one the caller may not read."""
