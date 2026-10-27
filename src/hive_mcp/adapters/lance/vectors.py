"""LanceDB VectorIndex: one table per embedding model, exact cosine search, ACL as a prefilter.

Vectors are derived data (the items are the source of truth), so a lost or stale table is
repaired by `hive reindex`. Replacing an item's chunks is delete-then-add under one lock; a crash
between the two leaves the item without vectors until its (retried) index job runs again.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from hive_mcp.adapters.lance.ann import INDEX_MIN_ROWS, NPROBE_FRACTION, AnnIndex
from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.domain.item import Hit
from hive_mcp.search.vectors import unit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from hive_mcp.domain.access import Acl, ScopeFilter

log = logging.getLogger(__name__)
INDEX_INFO_SECONDS = 30.0  # how long a table's index size is remembered
MIN_FETCH = 30
MAX_FETCH = 8192  # chunks examined to find `limit` distinct items
OPTIMIZE_EVERY = 100  # writes between compactions: every write adds a tiny file and a version


def quote(text: str) -> str:
    """A SQL string literal. Owners and ids are arbitrary text: quotes are doubled and control
    characters (which no real id contains) are refused rather than guessed at."""
    if any(ord(c) < 32 or ord(c) == 127 for c in text):  # noqa: PLR2004
        raise ValidationError("control characters are not allowed in scope names")
    return "'" + text.replace("'", "''") + "'"


def acl_filter(flt: ScopeFilter) -> str:
    """Tenant boundary AND (one of the caller's scopes OR shared org-wide). Only a prefilter:
    the tier is still decided by ScopeFilter.tier."""
    scopes = sorted((int(s.level), s.owner) for s in flt.member_scopes)
    own = " OR ".join(f"(scope_level = {lvl} AND scope_owner = {quote(o)})" for lvl, o in scopes)
    shared = "visibility = 'org'"
    return f"org_id = {quote(flt.org_id)} AND ({own + ' OR ' if own else ''}{shared})"


def table_name(model_id: str) -> str:
    return "vec_" + hashlib.sha256(model_id.encode()).hexdigest()[:24]


class LanceVectors:
    def __init__(
        self,
        directory: Path,
        *,
        index_min_rows: int = INDEX_MIN_ROWS,
        nprobe_fraction: float = NPROBE_FRACTION,
    ) -> None:
        try:
            import lancedb  # noqa: PLC0415 - optional extra
        except ImportError as exc:
            raise HiveError("HIVE_VECTOR_STORE=lancedb needs the 'lancedb' extra") from exc
        directory.mkdir(parents=True, exist_ok=True)
        self._db: Any = lancedb.connect(str(directory))
        self._lock = asyncio.Lock()
        self._writes = 0
        self._ann = AnnIndex(
            self._db, directory, min_rows=index_min_rows, nprobe_fraction=nprobe_fraction
        )

    # ------------------------------------------------------------------ tables
    def _open(self, model_id: str) -> Any | None:  # noqa: ANN401 - lancedb table
        name = table_name(model_id)
        if name not in self._db.list_tables().tables:
            return None
        return self._db.open_table(name)

    def _schema(self, model_id: str, dim: int) -> Any:  # noqa: ANN401 - pyarrow schema
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema(
            [
                pa.field("item_id", pa.string()),
                pa.field("ord", pa.int32()),
                pa.field("org_id", pa.string()),
                pa.field("scope_level", pa.int8()),
                pa.field("scope_owner", pa.string()),
                pa.field("visibility", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
            ],
            metadata={"model_id": model_id},  # table names are hashes: the model is named here
        )

    @staticmethod
    def _dim(table: Any) -> int:  # noqa: ANN401
        return int(table.schema.field("vector").type.list_size)

    # ------------------------------------------------------------------ writes
    async def replace(
        self, item_id: str, model_id: str, acl: Acl, vectors: Sequence[NDArray[np.float32]]
    ) -> None:
        mats = [unit(v) for v in vectors]  # validate before touching the store
        async with self._lock:
            await asyncio.to_thread(self._replace, item_id, model_id, acl, mats)

    def _compact(self, table: Any) -> None:  # noqa: ANN401
        """Merge the small files writes leave behind, or every search has to open all of them."""
        self._writes += 1
        if self._writes % OPTIMIZE_EVERY == 0:
            table.optimize()
            self._ann.maybe_build(table)

    def _replace(
        self, item_id: str, model_id: str, acl: Acl, mats: list[NDArray[np.float32]]
    ) -> None:
        table = self._open(model_id)
        if table is None and mats:
            if any(
                m.shape[0] != mats[0].shape[0] for m in mats
            ):  # before the table fixes a dimension
                raise ValidationError(f"model {model_id} has dimension {mats[0].shape[0]}")
            table = self._db.create_table(
                table_name(model_id), schema=self._schema(model_id, len(mats[0]))
            )
        if table is None:
            return
        if mats and any(m.shape[0] != self._dim(table) for m in mats):
            raise ValidationError(f"model {model_id} has dimension {self._dim(table)}")
        table.delete(f"item_id = {quote(item_id)}")
        if mats:
            rows = [
                {
                    "item_id": item_id, "ord": i, "org_id": acl.org_id,
                    "scope_level": int(acl.scope.level), "scope_owner": acl.scope.owner,
                    "visibility": str(acl.visibility), "vector": m.tolist(),
                }
                for i, m in enumerate(mats)
            ]  # fmt: skip
            table.add(rows)
        self._compact(table)

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update_acl, item_id, acl)

    def _update_acl(self, item_id: str, acl: Acl) -> None:
        values = {
            "org_id": acl.org_id, "scope_level": int(acl.scope.level),
            "scope_owner": acl.scope.owner, "visibility": str(acl.visibility),
        }  # fmt: skip
        for name in self._db.list_tables().tables:
            if name.startswith("vec_"):
                self._db.open_table(name).update(where=f"item_id = {quote(item_id)}", values=values)

    async def delete(self, item_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete, item_id)

    def _delete(self, item_id: str) -> None:
        for name in self._db.list_tables().tables:
            if name.startswith("vec_"):
                self._db.open_table(name).delete(f"item_id = {quote(item_id)}")

    # ------------------------------------------------------------------ reads
    async def search(
        self, flt: ScopeFilter, model_id: str, vector: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        q = unit(vector)
        return await asyncio.to_thread(self._search, flt, model_id, q, max(limit, 0))

    def _search(
        self, flt: ScopeFilter, model_id: str, q: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        table = self._open(model_id)
        if table is None or limit == 0:
            return []
        if q.shape[0] != self._dim(table):
            raise ValidationError(
                f"model {model_id} has dimension {self._dim(table)}, got {q.shape[0]}"
            )
        fetch = max(limit * 3, MIN_FETCH)
        where = acl_filter(flt)
        probes = self._ann.probes(table)
        while True:
            query = table.search(q).distance_type("cosine").where(where, prefilter=True)
            if probes:
                query = query.nprobes(probes)
            rows = query.limit(fetch).select(["item_id", "ord", "_distance"]).to_list()
            best: dict[str, tuple[float, int]] = {}
            for r in rows:  # rows come best first, so the first chunk seen per item is its best
                best.setdefault(r["item_id"], (1.0 - float(r["_distance"]), int(r["ord"])))
            if len(best) >= limit or len(rows) < fetch or fetch >= MAX_FETCH:
                break
            fetch *= 4  # many chunks of few items filled the page: look further
        hits = [Hit(item, score, ordinal) for item, (score, ordinal) in best.items()]
        hits.sort(key=lambda h: (-h.score, h.item_id))
        return hits[:limit]

    async def model_stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._model_stats)

    def _model_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for name in self._db.list_tables().tables:
            if not name.startswith("vec_"):
                continue
            table = self._db.open_table(name)
            model = (table.schema.metadata or {}).get(b"model_id", b"").decode()
            if model:
                stats[model] = len(self._item_ids(table))
        return stats

    @staticmethod
    def _item_ids(table: Any) -> set[str]:  # noqa: ANN401
        found = table.search().select(["item_id"]).limit(2**31 - 1).to_arrow()
        return set(found["item_id"].to_pylist())

    async def drop_model(self, model_id: str) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._drop_model, model_id)

    def _drop_model(self, model_id: str) -> int:
        table = self._open(model_id)
        if table is None:
            return 0
        count = len(self._item_ids(table))
        self._db.drop_table(table_name(model_id))
        return count
