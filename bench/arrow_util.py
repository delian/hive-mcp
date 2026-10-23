"""Arrow record batches of the benchmark dataset (shared by the embedded candidates)."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pyarrow as pa

from bench.common import DIM, Dataset

SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("group_id", pa.int32()),
        ("vector", pa.list_(pa.float32(), DIM)),
        ("body", pa.string()),
    ]
)


def batches(ds: Dataset, texts: list[str], step: int = 50_000) -> Iterator[pa.RecordBatch]:
    for lo in range(0, ds.n, step):
        hi = min(lo + step, ds.n)
        flat = pa.array(ds.f32(lo, hi).reshape(-1))
        yield pa.record_batch(
            [
                pa.array(np.arange(lo, hi, dtype=np.int64)),
                pa.array(ds.group[lo:hi]),
                pa.FixedSizeListArray.from_arrays(flat, DIM),
                pa.array(texts[lo:hi]),
            ],
            schema=SCHEMA,
        )
