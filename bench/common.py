"""Shared benchmark pieces: synthetic dataset, queries, exact ground truth, metrics."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DIM = 384
GROUPS = 2000
CLUSTERS = 1500
VOCAB = 20000
K = 10
DATA_ROOT = Path("/home/user/benchdata")


@dataclass
class Dataset:
    n: int
    vecs: np.ndarray  # float16 (n, DIM), unit-ish norm
    group: np.ndarray  # int32 (n,)
    cluster: np.ndarray  # int32 (n,)

    def f32(self, lo: int = 0, hi: int | None = None) -> np.ndarray:
        return self.vecs[lo:hi].astype(np.float32)


def _unit(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def make_dataset(n: int, seed: int = 7) -> Dataset:
    """Clustered unit vectors (embedding-like); ~80% of chunks of a topic live in one group."""
    rng = np.random.default_rng(seed)
    centers = _unit(rng.normal(size=(CLUSTERS, DIM)).astype(np.float32))
    weights = 1.0 / np.arange(1, CLUSTERS + 1) ** 0.8
    cluster = rng.choice(CLUSTERS, size=n, p=weights / weights.sum()).astype(np.int32)
    vecs = np.empty((n, DIM), dtype=np.float16)
    for lo in range(0, n, 200_000):
        hi = min(lo + 200_000, n)
        noise = rng.normal(size=(hi - lo, DIM)).astype(np.float32)
        vecs[lo:hi] = _unit(centers[cluster[lo:hi]] + 0.45 * _unit(noise)).astype(np.float16)
    home = cluster % GROUPS
    rand = rng.integers(0, GROUPS, size=n).astype(np.int32)
    group = np.where(rng.random(n) < 0.8, home, rand).astype(np.int32)
    return Dataset(n, vecs, group, cluster)


def words_for(cluster: np.ndarray, start: int) -> list[str]:
    """Deterministic pseudo-text: 8 topic words (per cluster) + 52 Zipf background words."""
    out = []
    for i, c in enumerate(cluster):
        rng = np.random.default_rng(start + i)
        topic = (c * 20 + rng.integers(0, 20, 8)) % VOCAB
        back = rng.zipf(1.3, 52)
        over = back > VOCAB  # resample the heavy tail instead of clipping it onto one word
        back[over] = rng.integers(1, VOCAB + 1, int(over.sum()))
        back = back - 1
        out.append(" ".join(f"w{w}" for w in np.concatenate([topic, back])))
    return out


@dataclass
class Queries:
    vecs: np.ndarray  # float32 (q, DIM)
    allowed: list[np.ndarray]  # groups visible to each query's principal
    source: np.ndarray  # index of the chunk each query was derived from
    lex: list[str]  # lexical query built from source chunk words
    gt_all: np.ndarray  # (q, K) exact ids, unfiltered
    gt_acl: np.ndarray  # (q, K) exact ids, restricted to allowed groups
    selectivity: float


def _merge(best_s: np.ndarray, best_i: np.ndarray, s: np.ndarray, base: int) -> None:
    """Fold a block of scores (q, blk) into running per-query top-K (in place)."""
    kk = min(K, s.shape[1])
    idx = np.argpartition(-s, kk - 1, axis=1)[:, :kk]
    cs = np.concatenate([best_s, np.take_along_axis(s, idx, 1)], axis=1)
    ci = np.concatenate([best_i, idx + base], axis=1)
    top = np.argsort(-cs, axis=1)[:, :K]
    best_s[:] = np.take_along_axis(cs, top, 1)
    best_i[:] = np.take_along_axis(ci, top, 1)


def _ground_truth(ds: Dataset, qv: np.ndarray, allow: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    q = len(qv)
    bs = [np.full((q, K), -np.inf, np.float32) for _ in range(2)]
    bi = [np.zeros((q, K), np.int64) for _ in range(2)]
    for lo in range(0, ds.n, 100_000):
        hi = min(lo + 100_000, ds.n)
        sims = qv @ ds.f32(lo, hi).T
        _merge(bs[0], bi[0], sims, lo)
        sims[~allow[:, ds.group[lo:hi]]] = -np.inf
        _merge(bs[1], bi[1], sims, lo)
    return bi[0], bi[1]


def _lex_query(w: list[str]) -> str:
    """2 topic words + the 2 rarest background words of the source chunk (selective, AND-able)."""
    rare = sorted(w[8:], key=lambda x: -int(x[1:]))[:2]
    return " ".join(w[:2] + rare)


def make_queries(ds: Dataset, q: int = 300, seed: int = 11) -> Queries:
    rng = np.random.default_rng(seed)
    src = rng.choice(ds.n, size=q, replace=False)
    qv = _unit(ds.f32()[src] + 0.25 * _unit(rng.normal(size=(q, DIM)).astype(np.float32)))
    allowed = [
        np.unique(np.concatenate([[ds.group[s]], rng.integers(0, GROUPS, 4)])).astype(np.int32)
        for s in src
    ]
    allow = np.zeros((q, GROUPS), dtype=bool)
    for i, g in enumerate(allowed):
        allow[i, g] = True
    gt_all, gt_acl = _ground_truth(ds, qv, allow)
    sel = [allow[i, ds.group].mean() for i in range(q)]
    words = [words_for(ds.cluster[s : s + 1], int(s))[0].split() for s in src]
    lex = [_lex_query(w) for w in words]
    return Queries(qv, allowed, src, lex, gt_all, gt_acl, float(np.mean(sel)))


def recall(found: list[list[int]], truth: np.ndarray) -> float:
    return float(np.mean([len(set(f) & set(t.tolist())) / K for f, t in zip(found, truth)]))


def timed(fn, items) -> tuple[list, dict[str, float]]:  # noqa: ANN001
    fn(items[0])  # warm-up
    res, lat = [], []
    for it in items:
        t = time.perf_counter()
        res.append(fn(it))
        lat.append((time.perf_counter() - t) * 1000)
    a = np.array(lat)
    return res, {"p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
                 "p99": float(np.percentile(a, 99))}


def load_texts(ds: Dataset) -> list[str]:
    """Pseudo-text per chunk, cached on disk (generation is slow in pure Python)."""
    path = DATA_ROOT / f"text_{ds.n}.txt"
    if path.exists():
        return path.read_text().split("\n")[: ds.n]
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for lo in range(0, ds.n, 50_000):
        out += words_for(ds.cluster[lo : lo + 50_000], lo)
    path.write_text("\n".join(out))
    return out
