"""Find LanceDB settings that reach comparable recall (about 0.95+) and what they cost."""

from __future__ import annotations

import sys
import time

from bench.common import make_dataset, make_queries, recall, timed
from bench.lance import Lance

n, nq = int(sys.argv[1]), 100
ds = make_dataset(n)
qs = make_queries(ds, nq)
lc = Lance()
lc.load(ds)
idx = list(range(nq))
parts = max(8, int(n**0.5))
configs = [("IVF_PQ", {"num_partitions": parts, "num_sub_vectors": 96}),
           ("IVF_FLAT", {"num_partitions": parts}),
           ("IVF_HNSW_SQ", {"num_partitions": max(8, parts // 4)})]
for kind, kw in configs:
    t = time.perf_counter()
    lc.tbl.create_index(metric="dot", index_type=kind, vector_column_name="vector", **kw)
    build = time.perf_counter() - t
    for nprobes in (20, 50, 150):
        for refine in ((None, 20) if kind == "IVF_PQ" else (None,)):
            def search(i, allowed, nprobes=nprobes, refine=refine):
                s = lc.tbl.search(qs.vecs[i], vector_column_name="vector").metric("dot").nprobes(nprobes)
                if refine:
                    s = s.refine_factor(refine)
                if allowed is not None:
                    s = s.where(f"group_id IN ({','.join(map(str, allowed.tolist()))})", prefilter=True)
                return [r["id"] for r in s.select(["id"]).limit(10).to_list()]
            ra, la = timed(lambda i: search(i, None), idx)
            rf, lf = timed(lambda i: search(i, qs.allowed[i]), idx)
            print(f"{kind:12} build={build:6.1f}s nprobes={nprobes:3} refine={refine} "
                  f"plain recall={recall(ra, qs.gt_all):.3f} p50={la['p50']:6.1f}ms | "
                  f"acl recall={recall(rf, qs.gt_acl):.3f} p50={lf['p50']:6.1f}ms", flush=True)
