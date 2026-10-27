"""In-memory VectorIndex: exact cosine search, one entry per (model, item)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Hit
from hive_mcp.search.vectors import unit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from hive_mcp.domain.access import Acl, ScopeFilter


class MemoryVectors:
    def __init__(self) -> None:
        self._dims: dict[str, int] = {}
        self._rows: dict[str, dict[str, tuple[Acl, NDArray[np.float32]]]] = {}

    def _check_dim(self, model_id: str, dim: int) -> None:
        if self._dims.setdefault(model_id, dim) != dim:
            raise ValidationError(
                f"model {model_id} has dimension {self._dims[model_id]}, got {dim}"
            )

    async def replace(
        self, item_id: str, model_id: str, acl: Acl, vectors: Sequence[NDArray[np.float32]]
    ) -> None:
        mats = [unit(v) for v in vectors]  # validate everything before touching state
        if mats:  # all-or-nothing: a rejected call must not fix the model's dimension either
            dim = self._dims.get(model_id, int(mats[0].shape[0]))
            if any(m.shape[0] != dim for m in mats):
                raise ValidationError(f"model {model_id} has dimension {dim}")
            self._dims[model_id] = dim
        rows = self._rows.setdefault(model_id, {})
        if mats:
            rows[item_id] = (acl, np.vstack(mats))
        else:
            rows.pop(item_id, None)

    async def search(
        self, flt: ScopeFilter, model_id: str, vector: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        q = unit(vector)
        if model_id in self._dims:
            self._check_dim(model_id, q.shape[0])
        hits = []
        for item_id, (acl, mat) in self._rows.get(model_id, {}).items():
            if flt.tier(acl) is None:
                continue
            sims = mat @ q
            best = int(np.argmax(sims))
            hits.append(Hit(item_id, float(sims[best]), best))
        hits.sort(key=lambda h: (-h.score, h.item_id))
        return hits[: max(limit, 0)]

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        for rows in self._rows.values():
            if item_id in rows:
                rows[item_id] = (acl, rows[item_id][1])

    async def model_stats(self) -> dict[str, int]:
        return {model: len(rows) for model, rows in self._rows.items()}

    async def drop_model(self, model_id: str) -> int:
        self._dims.pop(model_id, None)
        return len(self._rows.pop(model_id, {}))

    async def delete(self, item_id: str) -> None:
        for rows in self._rows.values():
            rows.pop(item_id, None)
