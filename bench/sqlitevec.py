"""SQLite + sqlite-vec (brute-force KNN with metadata filter) + FTS5 (BM25)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import numpy as np
import sqlite_vec

from bench.common import DIM, Dataset, load_texts

PATH = Path("/home/user/benchdata/sqlitevec.db")


class SqliteVec:
    name = "sqlite-vec(brute force)"

    def __init__(self) -> None:
        for p in PATH.parent.glob("sqlitevec.db*"):
            p.unlink()
        self.con = sqlite3.connect(PATH)
        self.con.enable_load_extension(True)
        sqlite_vec.load(self.con)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=OFF")

    def load(self, ds: Dataset) -> float:
        texts = load_texts(ds)
        t = time.perf_counter()
        self.con.execute(f"CREATE VIRTUAL TABLE v USING vec0(id integer primary key, group_id integer, embedding float[{DIM}] distance_metric=cosine)")
        self.con.execute("CREATE VIRTUAL TABLE f USING fts5(body, content='', tokenize='unicode61', detail=column)")
        for lo in range(0, ds.n, 20_000):
            hi = min(lo + 20_000, ds.n)
            vec = ds.f32(lo, hi)
            self.con.executemany("INSERT INTO v(id, group_id, embedding) VALUES (?,?,?)",
                                 ((lo + i, int(ds.group[lo + i]), vec[i].tobytes()) for i in range(hi - lo)))
            self.con.executemany("INSERT INTO f(rowid, body) VALUES (?,?)", ((lo + i, texts[lo + i]) for i in range(hi - lo)))
            self.con.commit()
        return time.perf_counter() - t

    def build_vector_index(self) -> float:
        return 0.0

    def build_text_index(self) -> float:
        return 0.0  # FTS5 index is built during load (reported inside load_s)

    def vec_search(self, q: np.ndarray, allowed: np.ndarray | None) -> list[int]:
        flt = "" if allowed is None else f"AND group_id IN ({','.join(map(str, allowed.tolist()))})"
        sql = f"SELECT id FROM v WHERE embedding MATCH ? AND k = 10 {flt}"
        return [r[0] for r in self.con.execute(sql, (q.astype(np.float32).tobytes(),))]

    def lex_search(self, text: str) -> list[int]:
        sql = "SELECT rowid FROM f WHERE f MATCH ? ORDER BY bm25(f) LIMIT 10"
        return [r[0] for r in self.con.execute(sql, (text,))]

    def sizes(self) -> dict[str, float]:
        return {"file_mb": PATH.stat().st_size / 1e6}
