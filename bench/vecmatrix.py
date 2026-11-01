"""Would an in-memory vector matrix scale? Memory, load time, scan latency and concurrency.

An exact scan of a float32 matrix with one BLAS matrix-vector product, an access mask by row
index, and top-k with argpartition. Run: uv run python -m bench.vecmatrix 100000 1000000
"""

from __future__ import annotations

import json
import sys
import threading
import time

import numpy as np

from bench.common import DIM, K, make_dataset

QUERIES = 30
VISIBLE = (0.003, 0.03, 0.3, 1.0)  # fraction of chunks a principal may see


def percentile(lat: list[float], p: float) -> float:
    return float(np.percentile(np.array(lat) * 1000, p))


def scan(matrix: np.ndarray, rows: np.ndarray | None, q: np.ndarray) -> np.ndarray:
    block = matrix if rows is None else matrix[rows]  # a gather copies only what is visible
    sims = block @ q
    top = np.argpartition(-sims, min(K, len(sims) - 1))[:K]
    return top[np.argsort(-sims[top])]


def timed(fn, qs) -> dict:  # noqa: ANN001
    fn(qs[0])
    lat = []
    for q in qs:
        t = time.perf_counter()
        fn(q)
        lat.append(time.perf_counter() - t)
    return {"p50_ms": round(percentile(lat, 50), 1), "p95_ms": round(percentile(lat, 95), 1)}


def concurrent(fn, qs, threads: int) -> float:  # noqa: ANN001
    """Queries per second with `threads` searches at once (the BLAS call releases the GIL)."""
    start = threading.Barrier(threads + 1)

    def work() -> None:
        start.wait()
        for q in qs:
            fn(q)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    [t.start() for t in pool]
    start.wait()
    t0 = time.perf_counter()
    [t.join() for t in pool]
    return round(threads * len(qs) / (time.perf_counter() - t0), 1)


def one(n: int) -> dict:
    ds = make_dataset(n)
    rng = np.random.default_rng(3)
    t = time.perf_counter()
    blob = ds.vecs.tobytes()  # what SQLite hands over: one blob per chunk, joined and viewed
    mat = np.frombuffer(blob, dtype=np.float16).reshape(n, DIM).astype(np.float32)
    load_s = time.perf_counter() - t
    qs = mat[rng.choice(n, QUERIES, replace=False)]
    out: dict = {
        "chunks": n,
        "matrix_gb_f32": round(mat.nbytes / 1e9, 2),
        "matrix_gb_f16": round(mat.nbytes / 2e9, 2),
        "convert_s": round(load_s, 2),
    }
    for fraction in VISIBLE:
        rows = None if fraction == 1.0 else np.sort(rng.choice(n, int(n * fraction), replace=False))
        out[f"visible_{fraction}"] = timed(lambda q, r=rows: scan(mat, r, q), qs)
    out["qps_full_1_2_4_threads"] = [
        concurrent(lambda q: scan(mat, None, q), qs[:10], t) for t in (1, 2, 4)
    ]
    rows = np.sort(rng.choice(n, int(n * 0.03), replace=False))
    out["qps_3pct_1_2_4_threads"] = [
        concurrent(lambda q, r=rows: scan(mat, r, q), qs[:10], t) for t in (1, 2, 4)
    ]
    return out


if __name__ == "__main__":
    results = [one(int(a)) for a in sys.argv[1:]]
    print(json.dumps(results, indent=1))
