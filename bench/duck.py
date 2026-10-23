"""DuckDB (embedded): exact vector scan over ARRAY columns + the fts extension (BM25)."""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import numpy as np

from bench.arrow_util import batches
from bench.common import DIM, Dataset, load_texts

PATH = Path("/home/user/benchdata/duck.db")


class Duck:
    name = "duckdb(exact scan)"

    def __init__(self) -> None:
        for p in (PATH, Path(str(PATH) + ".wal")):
            p.unlink(missing_ok=True)
        self.con = duckdb.connect(str(PATH))
        self.con.execute("SET threads=4")
        self.con.execute("INSTALL fts; LOAD fts")

    def load(self, ds: Dataset) -> float:
        texts = load_texts(ds)
        t = time.perf_counter()
        self.con.execute(
            f"CREATE TABLE chunks (id BIGINT, group_id INTEGER, vector FLOAT[{DIM}], body VARCHAR)"
        )
        for b in batches(ds, texts):
            self.con.register("b", b)
            self.con.execute("INSERT INTO chunks SELECT * FROM b")
            self.con.unregister("b")
        self.con.execute("CHECKPOINT")
        return time.perf_counter() - t

    def build_vector_index(self) -> float:
        return 0.0  # exact scan: no index

    def build_text_index(self) -> float:
        t = time.perf_counter()
        self.con.execute(
            "PRAGMA create_fts_index('chunks','id','body', stemmer='none', stopwords='none', ignore='(\\.|[^a-z0-9])+', lower=1, strip_accents=0, overwrite=1)"
        )
        return time.perf_counter() - t

    def vec_search(self, q: np.ndarray, allowed: np.ndarray | None) -> list[int]:
        flt = (
            "" if allowed is None else f"WHERE group_id IN ({','.join(map(str, allowed.tolist()))})"
        )
        sql = f"SELECT id FROM chunks {flt} ORDER BY array_inner_product(vector, ?::FLOAT[{DIM}]) DESC LIMIT 10"
        return [r[0] for r in self.con.execute(sql, [q.tolist()]).fetchall()]

    def lex_search(self, text: str) -> list[int]:
        sql = (
            "SELECT id FROM (SELECT id, fts_main_chunks.match_bm25(id, ?, conjunctive:=1) AS s FROM chunks) "
            "WHERE s IS NOT NULL ORDER BY s DESC LIMIT 10"
        )
        return [r[0] for r in self.con.execute(sql, [text]).fetchall()]

    def sizes(self) -> dict[str, float]:
        return {"file_mb": PATH.stat().st_size / 1e6}
