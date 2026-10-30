"""Found by the load test (scripts/loadtest.py): LanceDB keeps every old table version for 7 days,
and a single-item write makes two (delete + add), so 20k notes took 3.5 GB of disk. Compaction
must also drop the versions nobody can still be reading."""

from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("lancedb")

from hive_mcp.adapters.lance import vectors as lance_vectors
from hive_mcp.adapters.lance.vectors import LanceVectors
from hive_mcp.domain.access import Acl
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

ACL = Acl("org1", Scope(ScopeLevel.PROJECT, "p1"), Visibility.SCOPE)


def manifests(root: Path) -> int:
    return sum(1 for p in root.rglob("*.manifest"))


async def test_bug_load_2_compaction_removes_old_table_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(lance_vectors, "OPTIMIZE_EVERY", 20)
    monkeypatch.setattr(lance_vectors, "KEEP_VERSIONS", timedelta(0))
    store = LanceVectors(tmp_path / "lance")
    gen = np.random.default_rng(3)
    for n in range(120):  # each write is a delete plus an add: 240 versions without cleanup
        await store.replace(f"i{n}", "m", ACL, [gen.normal(size=8).astype(np.float32)])
    assert manifests(tmp_path / "lance") < 60
