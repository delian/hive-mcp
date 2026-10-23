"""Load the embedded real corpus and build natural queries (phrases from stored chunks)."""

from __future__ import annotations


from collections import Counter

import numpy as np

from bench.common import DIM, GROUPS, REAL_DIR, Dataset, Queries, ground_truth


def real_dataset(limit: int | None = None) -> Dataset:
    shards = sorted((REAL_DIR / "shards").glob("*.npy"))
    vecs = np.concatenate([np.load(p) for p in shards])
    n = min(len(vecs), limit or len(vecs))
    texts = (REAL_DIR / "texts.txt").read_text().split("\n")[:n]
    group = np.load(REAL_DIR / "group.npy")[:n]
    assert vecs.shape[1] == DIM
    return Dataset(n, vecs[:n], group, np.zeros(n, dtype=np.int32), texts)


def real_queries(ds: Dataset, nq: int, seed: int = 11) -> Queries:
    from bench.embed import Embedder

    rng = np.random.default_rng(seed)
    assert ds.texts is not None
    raw = (REAL_DIR / "raw.txt").read_text().split("\n")[: ds.n]
    src = rng.choice(ds.n, size=nq, replace=False)
    phrases = []
    for s in src:
        w = raw[s].split()
        start = int(rng.integers(len(w) // 4, max(len(w) // 4 + 1, len(w) - 12)))
        phrases.append(" ".join(w[start : start + 12]))
    qv = Embedder().encode(phrases)
    df = Counter(t for text in ds.texts for t in set(text.split()))
    lex = [" ".join(sorted(set(ds.texts[s].split()), key=lambda t: df[t])[:4]) for s in src]
    allowed = [
        np.unique(np.concatenate([[ds.group[s]], rng.integers(0, GROUPS, 4)])).astype(np.int32)
        for s in src
    ]
    allow = np.zeros((nq, GROUPS), dtype=bool)
    for i, g in enumerate(allowed):
        allow[i, g] = True
    gt_all, gt_acl = ground_truth(ds, qv, allow)
    sel = [allow[i, ds.group].mean() for i in range(nq)]
    return Queries(qv, allowed, src, lex, gt_all, gt_acl, float(np.mean(sel)))
