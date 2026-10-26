"""Operator view of the vector indexes: which embedding models hold vectors and how complete the
current one is. For switching embedders safely: reindex, check coverage, prune the old model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from hive_mcp.ports.embeddings import Embedder
    from hive_mcp.ports.indexes import VectorIndex
    from hive_mcp.ports.maintenance import ItemMaintenance

MIN_COVERAGE = 0.95


@dataclass(frozen=True, slots=True)
class VectorStatus:
    current_model: str
    current_items: int  # items with vectors from the current model
    indexable_items: (
        int  # non-archived items (some have no text to embed, so 100% may not be reached)
    )
    other_models: dict[str, int]  # model -> items with vectors from it

    @property
    def coverage(self) -> float:
        return (
            1.0
            if self.indexable_items == 0
            else min(self.current_items / self.indexable_items, 1.0)
        )


class VectorAdmin:
    def __init__(
        self, vectors: VectorIndex, maintenance: ItemMaintenance, embedder: Embedder
    ) -> None:
        self._vectors, self._maintenance, self._embedder = vectors, maintenance, embedder

    async def status(self) -> VectorStatus:
        stats = await self._vectors.model_stats()
        current = self._embedder.model_id
        return VectorStatus(
            current,
            stats.pop(current, 0),
            await self._maintenance.count_system(),
            stats,
        )

    async def prune(self, *, force: bool = False) -> dict[str, int]:
        """Drop vectors of every model except the current one. Refuses while the current model
        covers too little of the data: that would leave search without semantic results."""
        status = await self.status()
        if status.coverage < MIN_COVERAGE and not force:
            raise ValidationError(
                f"the current model ({status.current_model}) covers only "
                f"{status.coverage:.0%} of items; finish `hive reindex` first (or pass --force)"
            )
        return {m: await self._vectors.drop_model(m) for m in sorted(status.other_models)}
