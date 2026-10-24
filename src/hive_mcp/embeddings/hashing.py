"""Feature-hashing embedder: deterministic, dependency-free, no model download.

Captures word overlap only (a smarter bag of words), NOT meaning. Use it for tests and development;
production uses a real model (local ONNX or a hosted API).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np

from hive_mcp.domain.errors import ValidationError
from hive_mcp.search.tokenize import tokenize

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray


class HashingEmbedder:
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.model_id = f"hashing-{dim}"
        self.min_similarity = 0.25  # random bags of words rarely exceed this at 128+ dimensions

    def _one(self, text: str) -> NDArray[np.float32]:
        tokens = tokenize(text)
        if not tokens:
            raise ValidationError("cannot embed text without words")
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "little") % self.dim
            vec[slot] += 1.0 if digest[4] & 1 else -1.0
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:  # signed collisions can cancel out exactly; fall back to unsigned counts
            for token in tokens:
                vec[
                    int.from_bytes(
                        hashlib.blake2b(token.encode(), digest_size=4).digest(), "little"
                    )
                    % self.dim
                ] += 1.0
            norm = float(np.linalg.norm(vec))
        return vec / norm

    async def embed(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        return [self._one(t) for t in texts]
