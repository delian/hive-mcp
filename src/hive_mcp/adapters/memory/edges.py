"""In-memory EdgeRepository."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.edge import Edge


class MemoryEdges:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, str], Edge] = {}

    async def add(self, edge: Edge) -> bool:
        key = (edge.org_id, *edge.key)
        if key in self._rows:
            return False
        self._rows[key] = edge
        return True

    async def get(self, org_id: str, src: str, dst: str, relation: str) -> Edge | None:
        return self._rows.get((org_id, src, dst, relation))

    async def remove(self, org_id: str, src: str, dst: str, relation: str) -> bool:
        return self._rows.pop((org_id, src, dst, relation), None) is not None

    async def pseudonymise_person(self, org_id: str, person_id: str, alias: str) -> int:
        changed = 0
        for key, e in list(self._rows.items()):
            if e.org_id == org_id and e.author_id == person_id:
                self._rows[key] = replace(e, author_id=alias)
                changed += 1
        return changed

    async def remove_item(self, org_id: str, item_id: str) -> int:
        doomed = [
            k for k, e in self._rows.items() if e.org_id == org_id and item_id in (e.src, e.dst)
        ]
        for key in doomed:
            del self._rows[key]
        return len(doomed)

    async def list_page(
        self, org_id: str, *, after: tuple[str, str, str] | None = None, limit: int = 500
    ) -> list[Edge]:
        rows = [
            e
            for e in self._rows.values()
            if e.org_id == org_id and (after is None or e.key > after)
        ]
        return sorted(rows, key=lambda e: e.key)[:limit]

    async def incident(
        self,
        org_id: str,
        item_ids: Sequence[str],
        *,
        relations: frozenset[str] = frozenset(),
        limit: int = 200,
    ) -> list[Edge]:
        ids = set(item_ids)
        rows = [
            e
            for e in self._rows.values()
            if e.org_id == org_id
            and (e.src in ids or e.dst in ids)
            and (not relations or e.relation in relations)
        ]
        rows.sort(key=lambda e: (e.created_at, e.src, e.dst, e.relation))
        return rows[:limit]
