"""Regression tests for vector index adapters (run against every backend)."""

from __future__ import annotations

import numpy as np
import pytest

from hive_mcp.domain.access import Acl, read_filter
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.scope import Visibility
from tests.conformance.factory import ALICE, P1

ACL1 = Acl("org1", P1, Visibility.SCOPE)


def v(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=np.float32)


async def test_rejected_replace_does_not_fix_the_model_dimension(backend):
    """BUG-ADP-1: a replace rejected for mixed dimensions must leave the model untouched, so a
    later valid replace with another dimension still works (memory kept the first vector's
    dimension; LanceDB even created the table)."""
    with pytest.raises(ValidationError):
        await backend.vectors.replace("a", "m", ACL1, [v(1, 0), v(1, 0, 0)])
    assert await backend.vectors.model_stats() == {}
    await backend.vectors.replace("a", "m", ACL1, [v(1, 0, 0)])
    hits = await backend.vectors.search(read_filter(ALICE), "m", v(1, 0, 0), 5)
    assert [h.item_id for h in hits] == ["a"]


async def test_lance_rejected_replace_does_not_fix_the_model_dimension(tmp_path):
    """BUG-ADP-1 (LanceDB): same scenario against the LanceDB store."""
    pytest.importorskip("lancedb")
    from hive_mcp.adapters.lance.vectors import LanceVectors

    vectors = LanceVectors(tmp_path / "lance")
    with pytest.raises(ValidationError):
        await vectors.replace("a", "m", ACL1, [v(1, 0), v(1, 0, 0)])
    assert await vectors.model_stats() == {}
    await vectors.replace("a", "m", ACL1, [v(1, 0, 0)])
    assert [h.item_id for h in await vectors.search(read_filter(ALICE), "m", v(1, 0, 0), 5)] == [
        "a"
    ]
