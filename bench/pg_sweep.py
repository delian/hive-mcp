"""Tune pgvector HNSW: sweep ef_search on the current index, optionally rebuild with other m/ef."""

from __future__ import annotations

import sys
import time

from bench.common import make_dataset, make_queries, recall, timed
from bench.pg import Postgres, start_server

n, nq = int(sys.argv[1]), 200
m, efc = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (None, None)
ds = make_dataset(n)
qs = make_queries(ds, nq)
start_server()
pg = Postgres()
build = None
if m:
    pg.conn.execute("DROP INDEX IF EXISTS chunks_hnsw")
    t = time.perf_counter()
    pg.conn.execute(f"CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding halfvec_ip_ops) WITH (m={m}, ef_construction={efc})")
    build = time.perf_counter() - t
size = pg.conn.execute("SELECT pg_relation_size('chunks_hnsw')/1e6").fetchone()[0]
print(f"index m={m or 16} ef_construction={efc or 64} build={build} size={float(size):.0f}MB", flush=True)
pg.conn.execute("SET hnsw.iterative_scan = relaxed_order")
pg.conn.execute("SET hnsw.max_scan_tuples = 200000")
idx = list(range(nq))
for ef in (100, 200, 400, 800):
    pg.conn.execute(f"SET hnsw.ef_search = {ef}")
    ra, la = timed(lambda i: pg.vec_search(qs.vecs[i], None), idx)
    rf, lf = timed(lambda i: pg.vec_search(qs.vecs[i], qs.allowed[i]), idx)
    print(f"ef_search={ef:4} plain recall={recall(ra, qs.gt_all):.3f} p50={la['p50']:5.1f} p99={la['p99']:6.1f}ms | "
          f"acl recall={recall(rf, qs.gt_acl):.3f} p50={lf['p50']:5.1f} p99={lf['p99']:6.1f}ms", flush=True)
