"""LanceDB (embedded, on-disk columnar format; IVF vector index, native BM25 FTS)."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import lancedb
import numpy as np

from bench.arrow_util import SCHEMA, batches
from bench.common import Dataset, load_texts

PATH = Path("/home/user/benchdata/lance")
INDEX = os.environ.get("LANCE_INDEX", "IVF_FLAT")
NPROBES = int(os.environ.get("LANCE_NPROBES", "150"))
REFINE = int(os.environ.get("LANCE_REFINE", "10"))


class Lance:
    def __init__(self) -> None:
        self.name = f"lancedb/{INDEX}"
        shutil.rmtree(PATH, ignore_errors=True)
        self.db = lancedb.connect(str(PATH))
        self.n = 0

    def load(self, ds: Dataset) -> float:
        self.n = ds.n
        texts = load_texts(ds)
        t = time.perf_counter()
        self.tbl = self.db.create_table("chunks", data=batches(ds, texts), schema=SCHEMA)
        self.tbl.create_scalar_index("group_id", index_type="BITMAP")
        return time.perf_counter() - t

    def build_vector_index(self) -> float:
        t = time.perf_counter()
        parts = max(8, int(self.n**0.5))
        kw = {"num_partitions": parts, "num_sub_vectors": 48} if INDEX == "IVF_PQ" else {"num_partitions": parts}
        self.tbl.create_index(metric="dot", index_type=INDEX, vector_column_name="vector", **kw)
        return time.perf_counter() - t

    def build_text_index(self) -> float:
        t = time.perf_counter()
        self.tbl.create_fts_index("body", base_tokenizer="whitespace", stem=False,
                                  remove_stop_words=False, ascii_folding=False)
        return time.perf_counter() - t

    def vec_search(self, q: np.ndarray, allowed: np.ndarray | None) -> list[int]:
        s = self.tbl.search(q, vector_column_name="vector").metric("dot").nprobes(NPROBES)
        if INDEX == "IVF_PQ":
            s = s.refine_factor(REFINE)
        if allowed is not None:
            s = s.where(f"group_id IN ({','.join(map(str, allowed.tolist()))})", prefilter=True)
        return [r["id"] for r in s.select(["id"]).limit(10).to_list()]

    def lex_search(self, text: str) -> list[int]:
        return [r["id"] for r in self.tbl.search(text, query_type="fts").select(["id"]).limit(10).to_list()]

    def sizes(self) -> dict[str, float]:
        total = sum(f.stat().st_size for f in PATH.rglob("*") if f.is_file()) / 1e6
        return {"total_mb": total}
