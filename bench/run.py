"""Run one candidate through the whole benchmark and print a JSON result."""

from __future__ import annotations

import json
import os
import sys

from bench.common import K, make_dataset, make_queries, recall, timed

Q = 300


def make(name: str):  # noqa: ANN201
    if name == "pg":
        from bench.pg import Postgres, start_server
        start_server()
        return Postgres()
    if name == "lance":
        from bench.lance import Lance
        return Lance()
    if name == "duck":
        from bench.duck import Duck
        return Duck()
    if name == "sqlitevec":
        from bench.sqlitevec import SqliteVec
        return SqliteVec()
    raise SystemExit(f"unknown candidate {name}")


def main(name: str, n: int, nq: int = Q) -> dict:
    if os.environ.get("BENCH_REAL"):
        from bench.real_data import real_dataset, real_queries

        ds = real_dataset(n)
        n = ds.n
        qs = real_queries(ds, nq)
    else:
        ds = make_dataset(n)
        qs = make_queries(ds, nq)
    c = make(name)
    r: dict = {"candidate": c.name, "n": n, "dim": 384, "acl_selectivity": qs.selectivity}
    r["load_s"] = c.load(ds)
    r["vec_index_s"] = c.build_vector_index()
    r["fts_index_s"] = c.build_text_index()
    r["sizes"] = c.sizes()
    idx = list(range(nq))
    res, lat = timed(lambda i: c.vec_search(qs.vecs[i], None), idx)
    r["vec"] = {"recall@10": recall(res, qs.gt_all), **lat}
    res, lat = timed(lambda i: c.vec_search(qs.vecs[i], qs.allowed[i]), idx)
    r["vec_acl"] = {"recall@10": recall(res, qs.gt_acl), **lat}
    res, lat = timed(lambda i: c.lex_search(qs.lex[i]), idx)
    hit = sum(int(qs.source[i]) in res[i] for i in idx) / nq
    r["lex"] = {"source_hit@10": hit, **lat}
    return r


if __name__ == "__main__":
    out = main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else Q)
    print(json.dumps(out, indent=1))
    with open(f"/home/user/benchdata/result_{sys.argv[1]}_{sys.argv[2]}{'_real' if os.environ.get('BENCH_REAL') else ''}.json", "w") as f:
        json.dump(out, f, indent=1)
