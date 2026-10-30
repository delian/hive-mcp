"""Found by the load test (scripts/loadtest.py): the SQLite vector scan built and sorted a Hit for
every item in the search space (about half of a 50k-note search's time). The rewrite only touches
chunks that can still reach the top `limit`; this pins that it returns exactly what a brute-force
reference returns, ties and multi-chunk items included, across batch boundaries."""

import numpy as np
import pytest

from hive_mcp.adapters.sqlite import vectors as sqlite_vectors
from hive_mcp.domain.access import Acl, read_filter
from hive_mcp.domain.scope import Visibility
from tests.conformance.factory import ALICE, P1

ACL = Acl("org1", P1, Visibility.SCOPE)
MODEL = "load-model"
DIM = 6


def reference(chunks: dict[str, list[np.ndarray]], q: np.ndarray, limit: int):
    unit = lambda v: v / np.linalg.norm(v)  # noqa: E731
    best = {}
    for item, mats in chunks.items():
        sims = [float(np.float32(unit(m).astype("<f4") @ unit(q).astype("<f4"))) for m in mats]
        best[item] = max(sims)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


@pytest.mark.parametrize("limit", [1, 3, 8, 40, 500])
@pytest.mark.parametrize("exact_ties", [False, True])
async def test_bug_load_1_scan_matches_brute_force_across_batches(
    backend, monkeypatch, limit, exact_ties
):
    if type(backend).__name__ != "SqliteBackend":
        pytest.skip("the exact scan is SQLite's; pgvector is approximate by design")
    monkeypatch.setattr(sqlite_vectors, "BATCH", 16)  # several batches from a small corpus
    gen = np.random.default_rng(5)
    palette = gen.normal(size=(5, DIM)).astype(np.float32)  # few distinct vectors: many exact ties
    chunks: dict[str, list[np.ndarray]] = {}
    for n in reversed(range(120)):  # later rows have smaller ids: ties must still be ordered by id
        count = int(gen.integers(1, 5))
        mats = [palette[int(gen.integers(0, 5))].copy() for _ in range(count)]
        mats += [gen.normal(size=DIM).astype(np.float32)] if n % 3 == 0 else []
        chunks[f"item{n:03d}"] = mats
        await backend.vectors.replace(f"item{n:03d}", MODEL, ACL, mats)
    q = palette[0].copy() if exact_ties else gen.normal(size=DIM).astype(np.float32)
    hits = await backend.vectors.search(read_filter(ALICE), MODEL, q, limit)
    expected = reference(chunks, q, limit)
    assert [h.item_id for h in hits] == [item for item, _ in expected]
    assert [h.score for h in hits] == pytest.approx([score for _, score in expected], abs=1e-6)
    assert len({h.item_id for h in hits}) == len(hits)


async def test_bug_load_1_zero_limit_returns_nothing(backend):
    await backend.vectors.replace("a", MODEL, ACL, [np.ones(DIM, dtype=np.float32)])
    assert (
        await backend.vectors.search(read_filter(ALICE), MODEL, np.ones(DIM, np.float32), 0) == []
    )
