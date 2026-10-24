"""Search indexes. Both denormalise the item ACL: call `update_acl` after any relocate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray

    from hive_mcp.domain.access import Acl, ScopeFilter
    from hive_mcp.domain.item import Hit


@dataclass(frozen=True, slots=True)
class TextDoc:
    item_id: str
    acl: Acl
    text: str


class TextIndex(Protocol):
    async def upsert(self, doc: TextDoc) -> None: ...

    async def search(self, flt: ScopeFilter, query: str, limit: int) -> list[Hit]:
        """Case-insensitive for all Unicode scripts. Terms are OR-ed; more and rarer matches
        rank higher.
        One hit per item, best first. Only items visible under `flt` are returned."""

    async def update_acl(self, item_id: str, acl: Acl) -> None: ...

    async def delete(self, item_id: str) -> None: ...


class VectorIndex(Protocol):
    async def replace(
        self, item_id: str, model_id: str, acl: Acl, vectors: Sequence[NDArray[np.float32]]
    ) -> None:
        """Atomically replace ALL chunks of this item for this model (chunk ordinal = position).
        An empty sequence removes them. Vectors are L2-normalised by the adapter; a zero vector or a
        dimension that differs from the model's first vector raises ValidationError."""

    async def search(
        self, flt: ScopeFilter, model_id: str, vector: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        """Cosine similarity, one hit per item (best chunk), best first. Never crosses models."""

    async def update_acl(self, item_id: str, acl: Acl) -> None: ...

    async def delete(self, item_id: str) -> None:
        """Remove the item's chunks for every model."""
