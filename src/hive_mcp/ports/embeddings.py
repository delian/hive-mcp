"""Text -> vector. Implementations: hashing (dev/test), local ONNX and hosted APIs (later)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray


class Embedder(Protocol):
    model_id: str  # vectors from different model ids are never compared
    dim: int
    # Cosine similarity below which a hit counts as unrelated. Model-specific: nearest neighbours
    # always exist, so without a floor every query would return noise.
    min_similarity: float

    async def embed(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        """One finite non-zero vector per text, in order; ValidationError for wordless text."""

    async def embed_query(self, text: str) -> NDArray[np.float32]:
        """Embed a search query (some models want a different treatment than documents)."""
