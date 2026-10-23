"""PostgreSQL 16 + pgvector (halfvec HNSW) + built-in full-text search."""

from __future__ import annotations

import struct
import subprocess
import time

import numpy as np
import psycopg

from bench.common import DIM, Dataset, load_texts

PORT = 55432
DSN = f"host=/tmp port={PORT} dbname=bench user=postgres"
PG = "/usr/lib/postgresql/16/bin"
CONF = [
    "shared_buffers=4GB", "maintenance_work_mem=3GB", "max_parallel_maintenance_workers=3",
    "effective_cache_size=10GB", "work_mem=64MB", "fsync=off", "synchronous_commit=off",
    "max_wal_size=8GB", "checkpoint_timeout=30min", "listen_addresses=''",
    f"port={PORT}", "unix_socket_directories='/tmp'",
]


def start_server() -> None:
    st = subprocess.run(["su", "postgres", "-c", f"{PG}/pg_ctl -D /tmp/pgdata status"], capture_output=True)
    if st.returncode == 0:
        return
    args = " ".join(f"-c {c}" for c in CONF)
    subprocess.run(["su", "postgres", "-c", f"{PG}/pg_ctl -D /tmp/pgdata -o \"{args}\" -w -l /tmp/pg.log start"], check=True)
    subprocess.run(["su", "postgres", "-c", f"{PG}/createdb -h /tmp -p {PORT} bench"], check=False)


def stop_server() -> None:
    subprocess.run(["su", "postgres", "-c", f"{PG}/pg_ctl -D /tmp/pgdata -m fast stop"], check=False)


def _rows(ds: Dataset, texts: list[str], lo: int, hi: int) -> bytes:
    n = hi - lo
    fixed = np.zeros(n, dtype=[("nf", ">i2"), ("l0", ">i4"), ("id", ">i8"), ("l1", ">i4"),
                                ("g", ">i4"), ("l2", ">i4"), ("dim", ">i2"), ("un", ">i2"),
                                ("v", ">f2", (DIM,))])
    fixed["nf"], fixed["l0"], fixed["l1"], fixed["l2"] = 4, 8, 4, 4 + 2 * DIM
    fixed["id"] = np.arange(lo, hi)
    fixed["g"] = ds.group[lo:hi]
    fixed["dim"] = DIM
    fixed["v"] = ds.vecs[lo:hi]
    size = fixed.dtype.itemsize
    raw = fixed.tobytes()
    parts = []
    for i in range(n):
        body = texts[lo + i].encode()
        parts += [raw[i * size : (i + 1) * size], struct.pack(">i", len(body)), body]
    return b"".join(parts)


class Postgres:
    name = "postgres+pgvector"

    def __init__(self) -> None:
        self.conn = psycopg.connect(DSN, autocommit=True)

    def load(self, ds: Dataset) -> float:
        texts = load_texts(ds)
        c = self.conn
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("DROP TABLE IF EXISTS chunks")
        c.execute(f"CREATE TABLE chunks (id bigint, group_id int, embedding halfvec({DIM}), body text)")
        t = time.perf_counter()
        with c.cursor() as cur, cur.copy("COPY chunks FROM STDIN (FORMAT BINARY)") as cp:
            cp.write(b"PGCOPY\n\xff\r\n\0" + struct.pack(">ii", 0, 0))
            for lo in range(0, ds.n, 20_000):
                cp.write(_rows(ds, texts, lo, min(lo + 20_000, ds.n)))
            cp.write(struct.pack(">h", -1))
        c.execute("ALTER TABLE chunks ADD PRIMARY KEY (id)")
        c.execute("CREATE INDEX ON chunks (group_id)")
        c.execute("ANALYZE chunks")
        return time.perf_counter() - t

    def build_vector_index(self) -> float:
        t = time.perf_counter()
        self.conn.execute("CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding halfvec_ip_ops) WITH (m=16, ef_construction=64)")
        self.conn.execute("SET hnsw.ef_search = 100")
        self.conn.execute("SET hnsw.iterative_scan = relaxed_order")
        self.conn.execute("SET hnsw.max_scan_tuples = 200000")
        return time.perf_counter() - t

    def build_text_index(self) -> float:
        t = time.perf_counter()
        self.conn.execute("CREATE INDEX chunks_fts ON chunks USING gin (to_tsvector('simple', body))")
        return time.perf_counter() - t

    def vec_search(self, q: np.ndarray, allowed: np.ndarray | None) -> list[int]:
        lit = "[" + ",".join(f"{x:.5f}" for x in q) + "]"
        if allowed is None:
            sql = "SELECT id FROM chunks ORDER BY embedding <#> %s::halfvec LIMIT 10"
            args: tuple = (lit,)
        else:
            sql = "SELECT id FROM chunks WHERE group_id = ANY(%s) ORDER BY embedding <#> %s::halfvec LIMIT 10"
            args = (allowed.tolist(), lit)
        return [r[0] for r in self.conn.execute(sql, args)]

    def lex_search(self, text: str) -> list[int]:
        q = " & ".join(text.split())
        sql = ("SELECT id FROM chunks WHERE to_tsvector('simple', body) @@ to_tsquery('simple', %s) "
               "ORDER BY ts_rank(to_tsvector('simple', body), to_tsquery('simple', %s)) DESC LIMIT 10")
        return [r[0] for r in self.conn.execute(sql, (q, q))]

    def sizes(self) -> dict[str, float]:
        def mb(rel: str) -> float:
            return float(self.conn.execute(f"SELECT pg_relation_size('{rel}')/1e6").fetchone()[0])  # type: ignore[index]
        return {"table_mb": mb("chunks"), "hnsw_mb": mb("chunks_hnsw"), "fts_mb": mb("chunks_fts")}
