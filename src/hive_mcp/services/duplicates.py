"""Sweep for clusters of near-duplicate notes, so a team can merge them (`kb_merge`).

Works through the caller's own editable items a page at a time (a cursor continues the sweep),
asks the vector index for each one's close neighbours, and joins items that point at each other into
groups. Read-only: merging stays a deliberate step by whoever can edit the items.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Cursor, ItemQuery, Status

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.curation import CurationService

DEFAULT_SCAN = 50
MAX_SCAN = 200
DEFAULT_THRESHOLD = 0.9
NEIGHBOURS = 6


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    ids: tuple[str, ...]  # sorted, at least two
    best_score: float
    exact: bool  # identical content, not only close in meaning


@dataclass(frozen=True, slots=True)
class DuplicateSweep:
    groups: list[DuplicateGroup]
    scanned: int
    next_cursor: str | None  # pass it back to continue; None when the sweep is complete


class _Groups:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self.best: dict[tuple[str, str], float] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def join(self, a: str, b: str, score: float) -> None:
        self._parent[self.find(a)] = self.find(b)
        self.best[(min(a, b), max(a, b))] = score

    def members(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in list(self._parent):
            out.setdefault(self.find(x), []).append(x)
        return {root: sorted(ids) for root, ids in out.items() if len(ids) > 1}


class DuplicateFinder:
    def __init__(self, items: ItemRepository, curation: CurationService) -> None:
        self._items, self._curation = items, curation

    async def sweep(
        self,
        principal: Principal,
        *,
        after: str | None = None,
        scan: int = DEFAULT_SCAN,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> DuplicateSweep:
        if not 1 <= scan <= MAX_SCAN or not 0.5 <= threshold <= 1.0:  # noqa: PLR2004
            raise ValidationError(f"scan is 1-{MAX_SCAN} and threshold is 0.5-1.0")
        query = ItemQuery(
            statuses=frozenset({Status.ACTIVE}), include_referenced=False, limit=min(scan, 100)
        )
        if after:
            query = replace(query, after=Cursor.decode(after))
        groups, scanned, cursor = _Groups(), 0, None
        exact: set[str] = set()
        while scanned < scan:
            page = await self._items.list(principal, query)
            for view in page.items:
                scanned += 1
                item = view.item
                if not access.can_write(principal, item.acl, item.author_id):
                    continue
                for s in await self._curation.similar(
                    principal, item.id, limit=NEIGHBOURS, min_score=threshold
                ):
                    other = s.view.item
                    if s.view.tier.value != "primary" or other.status is not Status.ACTIVE:
                        continue
                    groups.join(item.id, other.id, s.score)
                    if other.content_hash == item.content_hash:
                        exact.update((item.id, other.id))
            cursor = page.next_cursor
            if not cursor:
                break
            query = replace(query, after=Cursor.decode(cursor))
        found = [
            DuplicateGroup(
                tuple(ids),
                max(v for (a, b), v in groups.best.items() if a in ids and b in ids),
                all(i in exact for i in ids),
            )
            for ids in groups.members().values()
        ]
        found.sort(key=lambda g: (-len(g.ids), -g.best_score, g.ids))
        return DuplicateSweep(found, scanned, cursor)
