"""Vector helpers shared by every VectorIndex adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from numpy.typing import NDArray


def unit(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    """L2-normalise; rejects empty, non-finite and zero vectors (they poison every later search)."""
    v = np.asarray(vector, dtype=np.float32)
    if v.ndim != 1 or v.size == 0 or not np.all(np.isfinite(v)):
        raise ValidationError("vector must be a non-empty 1-D array of finite numbers")
    norm = float(np.linalg.norm(v))
    if norm == 0.0:
        raise ValidationError("zero vector")
    return v / norm
