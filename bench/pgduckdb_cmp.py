"""pgvector (HNSW and exact) against pg_duckdb (heap and Parquet) on the same data and queries.

Run: uv run python -m bench.pgduckdb_cmp 100000 [300000]   (needs the server of bench/pg.py with
pg_duckdb in shared_preload_libraries and `CREATE EXTENSION pg_duckdb` in database `bench`).
"""

# ruff: noqa: S608, S108
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import psycopg

from bench.common import DIM, make_dataset, make_queries, recall
from bench.pg import DSN, Postgres, _rows

PARQUET = Path("/tmp/bench_chunks.parquet")
CLIENTS = 4


def lit(q: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.5f}" for x in q) + "]"


def stats(lat: list[float]) -> dict[str, float]:
    a = np.array(lat) * 1000
    return {
        "p50_ms": round(float(np.percentile(a, 50)), 1),
        "p95_ms": round(float(np.percentile(a, 95)), 1),
    }


def run(search, qs, allowed, truth) -> dict:  # noqa: ANN001
    search(qs[0], allowed[0] if allowed is not None else None)
    found, lat = [], []
    for i, q in enumerate(qs):
        t = time.perf_counter()
        found.append(search(q, allowed[i] if allowed is not None else None))
        lat.append(time.perf_counter() - t)
    return {"recall@10": round(recall(found, truth), 3), **stats(lat)}


def qps(make_search, qs, allowed, clients: int = CLIENTS, each: int = 20) -> float:  # noqa: ANN001
    barrier = threading.Barrier(clients + 1)

    def work() -> None:
        search = make_search()
        barrier.wait()
        for i in range(each):
            search(qs[i % len(qs)], allowed[i % len(qs)] if allowed is not None else None)

    pool = [threading.Thread(target=work) for _ in range(clients)]
    [t.start() for t in pool]
    barrier.wait()
    t0 = time.perf_counter()
    [t.join() for t in pool]
    return round(clients * each / (time.perf_counter() - t0), 1)


def pgvector_search(conn, exact: bool):  # noqa: ANN001, ANN201
    conn.execute("SET hnsw.ef_search = 200")
    conn.execute("SET hnsw.iterative_scan = relaxed_order")
    conn.execute("SET hnsw.max_scan_tuples = 200000")
    conn.execute(f"SET enable_indexscan = {'off' if exact else 'on'}")
    conn.execute(f"SET enable_bitmapscan = {'off' if exact else 'on'}")

    def search(q, allowed):  # noqa: ANN001, ANN202
        if allowed is None:
            sql = "SELECT id FROM chunks ORDER BY embedding <#> %s::halfvec LIMIT 10"
            args: tuple = (lit(q),)
        else:
            sql = (
                "SELECT id FROM chunks WHERE group_id = ANY(%s) "
                "ORDER BY embedding <#> %s::halfvec LIMIT 10"
            )
            args = (allowed.tolist(), lit(q))
        return [r[0] for r in conn.execute(sql, args)]

    return search


def duck_search(conn, source: str, fn: str, cast: str):  # noqa: ANN001, ANN201
    def search(q, allowed):  # noqa: ANN001, ANN202
        where = "" if allowed is None else f"WHERE group_id IN ({','.join(map(str, allowed))})"
        inner = (
            f"SELECT id FROM {source} {where} ORDER BY {fn}(emb, {lit(q)}::{cast}) DESC LIMIT 10"
        )
        return [r[0] for r in conn.execute(f"SELECT id FROM duckdb.query($${inner}$$)")]

    return search


def build_tables(ds, c) -> dict:  # noqa: ANN001
    out: dict = {}
    pg = Postgres()
    c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    c.execute("DROP TABLE IF EXISTS chunks")
    c.execute(f"CREATE TABLE chunks (id bigint, group_id int, embedding halfvec({DIM}), body text)")
    t = time.perf_counter()
    import struct

    with c.cursor() as cur, cur.copy("COPY chunks FROM STDIN (FORMAT BINARY)") as cp:
        texts = [""] * ds.n
        cp.write(b"PGCOPY\n\xff\r\n\0" + struct.pack(">ii", 0, 0))
        for lo in range(0, ds.n, 20_000):
            cp.write(_rows(ds, texts, lo, min(lo + 20_000, ds.n)))
        cp.write(struct.pack(">h", -1))
    c.execute("ALTER TABLE chunks ADD PRIMARY KEY (id)")
    c.execute("CREATE INDEX ON chunks (group_id)")
    c.execute("ANALYZE chunks")
    out["load_s"] = round(time.perf_counter() - t, 1)
    t = time.perf_counter()
    c.execute(
        "CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding halfvec_ip_ops) "
        "WITH (m=32, ef_construction=200)"
    )
    out["hnsw_build_s"] = round(time.perf_counter() - t, 1)
    t = time.perf_counter()
    c.execute("DROP TABLE IF EXISTS chunks_arr")
    c.execute(
        "CREATE TABLE chunks_arr AS SELECT id, group_id, embedding::real[] AS emb FROM chunks"
    )
    c.execute("ALTER TABLE chunks_arr ALTER COLUMN emb SET DATA TYPE real[]")
    c.execute("ANALYZE chunks_arr")
    out["real_array_table_s"] = round(time.perf_counter() - t, 1)
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = time.perf_counter()
    flat = pa.array(ds.f32().ravel(), type=pa.float32())
    table = pa.table(
        {
            "id": pa.array(np.arange(ds.n), type=pa.int64()),
            "group_id": pa.array(ds.group, type=pa.int32()),
            "emb": pa.FixedSizeListArray.from_arrays(flat, DIM),
        }
    )
    PARQUET.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, PARQUET, compression="zstd", row_group_size=100_000)
    PARQUET.chmod(0o644)
    out["parquet_s"] = round(time.perf_counter() - t, 1)
    size = lambda rel: round(
        float(c.execute(f"SELECT pg_relation_size('{rel}')/1e6").fetchone()[0]), 1
    )  # noqa: E731
    out["sizes_mb"] = {
        "chunks_halfvec": size("chunks"),
        "hnsw": size("chunks_hnsw"),
        "chunks_arr": size("chunks_arr"),
        "parquet": round(PARQUET.stat().st_size / 1e6, 1),
    }
    del pg
    return out


def compare(n: int) -> dict:
    ds = make_dataset(n)
    qs = make_queries(ds, 100)
    c = psycopg.connect(DSN, autocommit=True)
    r: dict = {"chunks": n, "acl_selectivity": round(qs.selectivity, 4), **build_tables(ds, c)}
    arr = f"read_parquet('{PARQUET}')"
    candidates = {
        "pgvector_hnsw": lambda conn: pgvector_search(conn, exact=False),
        "pgvector_exact": lambda conn: pgvector_search(conn, exact=True),
        "pg_duckdb_heap_real_array": lambda conn: duck_search(
            conn, "chunks_arr", "list_inner_product", "FLOAT[]"
        ),
        "pg_duckdb_parquet_fixed_array": lambda conn: duck_search(
            conn, arr, "array_inner_product", f"FLOAT[{DIM}]"
        ),
    }
    for name, make in candidates.items():
        try:
            conn = psycopg.connect(DSN, autocommit=True)
            search = make(conn)
            entry = {
                "unfiltered": run(search, qs.vecs, None, qs.gt_all),
                "acl": run(search, qs.vecs, qs.allowed, qs.gt_acl),
            }
            entry["qps_4_clients_unfiltered"] = qps(
                lambda m=make: m(psycopg.connect(DSN, autocommit=True)), qs.vecs, None
            )
            entry["qps_4_clients_acl"] = qps(
                lambda m=make: m(psycopg.connect(DSN, autocommit=True)), qs.vecs, qs.allowed
            )
        except Exception as exc:  # noqa: BLE001 - report which candidate failed and why
            entry = {"error": str(exc)[:300]}
        r[name] = entry
        print(name, json.dumps(entry), flush=True)
    return r


if __name__ == "__main__":
    results = [compare(int(a)) for a in sys.argv[1:]]
    print(json.dumps(results, indent=1))
