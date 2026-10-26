"""Second-stage ranking: a cross-encoder reads the query and each candidate together."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class Reranker(Protocol):
    model_id: str
    # Scores below this mean "unrelated" (cross-encoder logits are comparable across queries).
    min_score: float

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage, in order (higher = more relevant)."""
