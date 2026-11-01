"""The in-memory vector matrices of a SQLite deployment, kept in step with the database.

Every change to `vec_chunks` (by any connection, process or cascade) is recorded in `vec_log` by
triggers. Before a search the cache reads the newest log number inside the same read transaction
as its data, and re-reads only the items logged since it last looked; if too much changed, or
the log was trimmed past it, it reloads the model. A model whose rows would not fit the memory
cap is left to the plain SQL scan.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from hive_mcp.adapters.sqlite.vector_matrix import Matrix, row_bytes

if TYPE_CHECKING:
    import sqlite3

    from numpy.typing import NDArray

    from hive_mcp.domain.access import ScopeFilter

log = logging.getLogger("hive.vectors")
BATCH = 8192
RELOAD_SHARE = 0.2  # more than this share of the items changed: reload instead of patching
RELOAD_MIN = 1000
KEEP_LOG = 50_000  # entries kept in vec_log (older ones are trimmed by the writers)
COLUMNS = "item_id, ord, org_id, scope_level, scope_owner, visibility, vec"


class TooBig(Exception):
    """The vectors of the model do not fit the memory cap."""


def trim_log(conn: sqlite3.Connection) -> None:
    """Writers call this in their transaction so the change log stays bounded."""
    conn.execute("DELETE FROM vec_log WHERE seq < (SELECT MAX(seq) FROM vec_log) - ?", (KEEP_LOG,))


class ModelCache:
    def __init__(self, model_id: str, dim: int, cap_bytes: int) -> None:
        self.model_id, self.dim, self.cap = model_id, dim, cap_bytes
        self.matrix: Matrix | None = None
        self.seq = 0
        self.lock = threading.Lock()

    def refresh(self, conn: sqlite3.Connection) -> None:
        newest = int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM vec_log").fetchone()[0])
        if self.matrix is not None and newest == self.seq:
            return
        oldest = conn.execute("SELECT MIN(seq) FROM vec_log").fetchone()[0]
        if self.matrix is None or (oldest is not None and oldest > self.seq + 1):
            return self._load(conn, newest)  # first use, or the log no longer reaches back
        changed = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT item_id FROM vec_log WHERE model_id = ? AND seq > ?",
                (self.model_id, self.seq),
            )
        ]
        if len(changed) > max(RELOAD_MIN, RELOAD_SHARE * len(self.matrix.item_index)):
            return self._load(conn, newest)
        for item_id in changed:
            self.matrix.remove(item_id)
            self._add_item(conn, self.matrix, item_id)
        if self.matrix.needs_compaction:
            self.matrix.compact()
        if self.matrix.nbytes > self.cap:
            raise TooBig
        self.seq = newest
        return None

    def _add_item(self, conn: sqlite3.Connection, matrix: Matrix, item_id: str) -> None:
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM vec_chunks WHERE item_id = ? AND model_id = ? ORDER BY ord",  # noqa: S608
            (item_id, self.model_id),
        ).fetchall()
        if rows:
            _add_rows(matrix, self.dim, rows)

    def _load(self, conn: sqlite3.Connection, newest: int) -> None:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM vec_chunks WHERE model_id = ?", (self.model_id,)
            ).fetchone()[0]
        )
        if total * row_bytes(self.dim) > self.cap:
            raise TooBig
        matrix = Matrix(self.dim, capacity=int(total * 1.05) + 1)
        cur = conn.execute(
            f"SELECT {COLUMNS} FROM vec_chunks WHERE model_id = ? ORDER BY item_id, ord",  # noqa: S608
            (self.model_id,),
        )
        while rows := cur.fetchmany(BATCH):
            _add_rows(matrix, self.dim, rows)
        if matrix.nbytes > self.cap:
            raise TooBig
        self.matrix, self.seq = matrix, newest

    def search(
        self, conn: sqlite3.Connection, flt: ScopeFilter, query: NDArray[np.float32], limit: int
    ) -> list[tuple[str, float, int]]:
        with self.lock:
            self.refresh(conn)
            assert self.matrix is not None  # noqa: S101 - set by refresh
            member = ((int(s.level), s.owner) for s in flt.member_scopes)
            return self.matrix.search(flt.org_id, member, query, limit)


def _add_rows(matrix: Matrix, dim: int, rows: list[tuple[object, ...]]) -> None:
    """Rows arrive grouped by item (ordered by item, ordinal); add each group in one call."""
    start = 0
    while start < len(rows):
        item_id = rows[start][0]
        stop = start
        while stop < len(rows) and rows[stop][0] == item_id:
            stop += 1
        group = rows[start:stop]
        first = group[0]
        vectors = np.frombuffer(b"".join([r[6] for r in group]), dtype="<f4").reshape(-1, dim)  # type: ignore[misc]
        matrix.add(
            str(item_id),
            [int(r[1]) for r in group],  # type: ignore[call-overload]
            vectors,
            (str(first[2]), int(first[3]), str(first[4])),  # type: ignore[call-overload]
            first[5] == "org",
        )
        start = stop


class VectorCache:
    """One ModelCache per model; `search` returns None when the plain scan should answer."""

    def __init__(self, cap_mb: int) -> None:
        self.cap = cap_mb * 1024 * 1024
        self._models: dict[str, ModelCache] = {}
        self._off: set[str] = set()
        self._guard = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.cap > 0

    def search(  # noqa: PLR0913, PLR0917 - one value each: where, who, what, how many
        self,
        conn: sqlite3.Connection,
        flt: ScopeFilter,
        model_id: str,
        dim: int,
        query: NDArray[np.float32],
        limit: int,
    ) -> list[tuple[str, float, int]] | None:
        if not self.enabled or model_id in self._off:
            return None
        with self._guard:
            cache = self._models.get(model_id)
            if cache is None or cache.dim != dim:
                cache = self._models[model_id] = ModelCache(model_id, dim, self.cap)
        try:
            return cache.search(conn, flt, query, limit)
        except TooBig:
            log.warning(
                "vector cache: model %s does not fit HIVE_VECTOR_CACHE_MB=%d; searching the "
                "database instead",
                model_id,
                self.cap // (1024 * 1024),
            )
            self._off.add(model_id)
            self._models.pop(model_id, None)
            return None

    def forget(self, model_id: str) -> None:
        """The model was dropped here (other processes find out through the change log)."""
        self._models.pop(model_id, None)
        self._off.discard(model_id)
