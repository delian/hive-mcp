"""Store for graph edges. No access control here: the graph service filters by item visibility."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.edge import Edge


class EdgeRepository(Protocol):
    async def pseudonymise_person(self, org_id: str, person_id: str, alias: str) -> int:
        """Replace a person's id with an alias in the records that stay; returns rows changed."""

    async def add(self, edge: Edge) -> bool:
        """Idempotent per (org, src, dst, relation); the first author is kept. True if new."""

    async def get(self, org_id: str, src: str, dst: str, relation: str) -> Edge | None: ...

    async def remove(self, org_id: str, src: str, dst: str, relation: str) -> bool: ...

    async def remove_item(self, org_id: str, item_id: str) -> int:
        """Delete every edge that starts or ends at the item; returns how many."""

    async def list_page(
        self, org_id: str, *, after: tuple[str, str, str] | None = None, limit: int = 500
    ) -> list[Edge]:
        """Every edge of the org, ascending by (src, dst, relation), strictly after the `after`
        key. System-level (no visibility filter): for export and maintenance only."""

    async def incident(
        self,
        org_id: str,
        item_ids: Sequence[str],
        *,
        relations: frozenset[str] = frozenset(),
        limit: int = 200,
    ) -> list[Edge]:
        """Edges with either end in `item_ids` (outgoing and incoming), this org only,
        optionally limited to `relations`; ordered by (created_at, src, dst, relation)."""
