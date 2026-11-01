"""All chunk vectors of one model as one float32 matrix: exact search without reading the database.

One row per chunk, next to small per-row arrays (scope id, visible-to-the-org flag, live flag,
item, ordinal). A search builds the access mask from those arrays (the same rule as the SQL
filter: same organisation and either a scope the caller belongs to or `visibility = org`), then
takes one matrix-vector product, over the visible rows only when they are few (5% or less) and
over every row otherwise, and picks the best items. Rows of a changed item are marked dead and
appended again; dead rows are squeezed out when they pile up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable

    from numpy.typing import NDArray

GATHER_FRACTION = 0.05  # at most this share visible: multiply only those rows
GROWTH = 1.25
START_ROWS = 1024
ROW_META_BYTES = 14  # sid 4 + item 4 + ordinal 4 + two flags
FIRST_PICK = 4  # chunks examined per wanted item before the exactness check
COMPACT_DEAD = 0.25
COMPACT_MIN = 1000


def row_bytes(dim: int) -> int:
    return dim * 4 + ROW_META_BYTES


class Matrix:
    def __init__(self, dim: int, capacity: int = START_ROWS) -> None:
        self.dim, self.n, self.dead = dim, 0, 0
        self._alloc(max(capacity, START_ROWS))
        self.item_names: list[str] = []
        self.item_index: dict[str, int] = {}
        self.rows_of: dict[int, list[int]] = {}
        self.scope_ids: dict[tuple[str, int, str], int] = {}
        self.sids_by_org: dict[str, list[int]] = {}

    def _alloc(self, capacity: int) -> None:
        self.capacity = capacity
        self.mat = np.empty((capacity, self.dim), dtype=np.float32)
        self.sid = np.empty(capacity, dtype=np.int32)
        self.item = np.empty(capacity, dtype=np.int32)
        self.ordinal = np.empty(capacity, dtype=np.int32)
        self.org_wide = np.zeros(capacity, dtype=bool)
        self.live = np.zeros(capacity, dtype=bool)

    @property
    def nbytes(self) -> int:
        return self.capacity * row_bytes(self.dim)

    def _grow(self, needed: int) -> None:
        old = (self.mat, self.sid, self.item, self.ordinal, self.org_wide, self.live)
        used = self.n
        self._alloc(max(int(self.capacity * GROWTH), needed))
        for new, previous in zip(
            (self.mat, self.sid, self.item, self.ordinal, self.org_wide, self.live),
            old,
            strict=True,
        ):
            new[:used] = previous[:used]
        self.live[used:] = False

    def _scope(self, org: str, level: int, owner: str) -> int:
        key = (org, level, owner)
        found = self.scope_ids.get(key)
        if found is None:
            found = self.scope_ids[key] = len(self.scope_ids)
            self.sids_by_org.setdefault(org, []).append(found)
        return found

    def _item(self, item_id: str) -> int:
        found = self.item_index.get(item_id)
        if found is None:
            found = self.item_index[item_id] = len(self.item_names)
            self.item_names.append(item_id)
        return found

    def add(
        self,
        item_id: str,
        ordinals: Iterable[int],
        vectors: NDArray[np.float32],
        scope: tuple[str, int, str],
        org_wide: bool,
    ) -> None:
        """Append one item's chunks (the caller removed the old ones first)."""
        count = len(vectors)
        if not count:
            return
        if self.n + count > self.capacity:
            self._grow(self.n + count)
        lo, hi = self.n, self.n + count
        self.mat[lo:hi] = vectors
        self.sid[lo:hi] = self._scope(*scope)
        idx = self._item(item_id)
        self.item[lo:hi] = idx
        self.ordinal[lo:hi] = list(ordinals)
        self.org_wide[lo:hi] = org_wide
        self.live[lo:hi] = True
        self.rows_of.setdefault(idx, []).extend(range(lo, hi))
        self.n = hi

    def remove(self, item_id: str) -> None:
        idx = self.item_index.get(item_id)
        rows = self.rows_of.pop(idx, None) if idx is not None else None
        if rows:
            self.live[rows] = False
            self.dead += len(rows)

    @property
    def needs_compaction(self) -> bool:
        return self.dead >= COMPACT_MIN and self.dead > COMPACT_DEAD * self.n

    def compact(self) -> None:
        keep = np.flatnonzero(self.live[: self.n])
        remap = np.full(self.n, -1, dtype=np.int64)
        remap[keep] = np.arange(len(keep))
        old = (self.mat, self.sid, self.item, self.ordinal, self.org_wide)
        self._alloc(max(int(len(keep) * GROWTH), START_ROWS))
        for new, previous in zip(
            (self.mat, self.sid, self.item, self.ordinal, self.org_wide), old, strict=True
        ):
            new[: len(keep)] = previous[keep]
        self.live[: len(keep)] = True
        self.rows_of = {i: [int(remap[r]) for r in rows] for i, rows in self.rows_of.items()}
        self.n, self.dead = len(keep), 0

    def _visible(self, org: str, member: Iterable[tuple[int, str]]) -> NDArray[np.bool_]:
        sids = len(self.scope_ids)
        member_ok = np.zeros(sids, dtype=bool)
        for level, owner in member:
            sid = self.scope_ids.get((org, level, owner))
            if sid is not None:
                member_ok[sid] = True
        org_ok = np.zeros(sids, dtype=bool)
        org_ok[self.sids_by_org.get(org, [])] = True
        row_sid = self.sid[: self.n]
        wide = self.org_wide[: self.n] & org_ok[row_sid]
        return self.live[: self.n] & (member_ok[row_sid] | wide)

    def search(
        self, org: str, member: Iterable[tuple[int, str]], query: NDArray[np.float32], limit: int
    ) -> list[tuple[str, float, int]]:
        """(item id, best chunk score, that chunk's ordinal), best first, ties by item id."""
        visible = self._visible(org, member)
        count = int(np.count_nonzero(visible))
        if count == 0 or limit <= 0:
            return []
        if count == self.n:
            rows, sims = None, self.mat[: self.n] @ query
        elif count <= GATHER_FRACTION * self.n:
            rows = np.flatnonzero(visible)
            sims = self.mat[rows] @ query
        else:
            rows = np.flatnonzero(visible)
            sims = (self.mat[: self.n] @ query)[rows]
        items = self.item[: self.n] if rows is None else self.item[rows]
        ordinals = self.ordinal[: self.n] if rows is None else self.ordinal[rows]
        return self._best(sims, items, ordinals, limit)

    def _best(
        self, sims: NDArray[np.float32], items: NDArray[np.int32], ordinals: NDArray[np.int32],
        limit: int,
    ) -> list[tuple[str, float, int]]:  # fmt: skip
        """Best chunk per item, then the top `limit` items. Looks at the best few chunks and widens
        until the answer cannot change: every unseen chunk must score strictly below the
        `limit`-th item (a tie could still reorder by item id)."""
        total = len(sims)
        take = min(total, limit * FIRST_PICK)
        while True:
            if take < total:
                top = np.argpartition(-sims, take - 1)[:take]
                cutoff = float(sims[top].min())
            else:
                top, cutoff = np.arange(total), -np.inf
            top = top[np.lexsort((ordinals[top], -sims[top]))]  # best score first, then ordinal
            best: dict[int, tuple[float, int]] = {}
            for pos in top.tolist():
                best.setdefault(int(items[pos]), (float(sims[pos]), int(ordinals[pos])))
            ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], self.item_names[kv[0]]))
            if take >= total or (len(ranked) >= limit and ranked[limit - 1][1][0] > cutoff):
                return [(self.item_names[i], s, o) for i, (s, o) in ranked[:limit]]
            take = min(total, take * FIRST_PICK)
