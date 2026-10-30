"""SQLite VectorIndex: exact cosine search over float32 blobs, streamed in batches.

Fine for the embedded profile (about 130 ms at 220k chunks); larger deployments use a scalable adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from hive_mcp.adapters.sqlite.acl import acl_where
from hive_mcp.adapters.sqlite.codec import acl_params
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Hit
from hive_mcp.search.vectors import unit

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.access import Acl, ScopeFilter

BATCH = 8192


def _model_dim(conn: sqlite3.Connection, model_id: str) -> int | None:
    row = conn.execute("SELECT dim FROM vec_models WHERE model_id = ?", (model_id,)).fetchone()
    return int(row["dim"]) if row else None


class SqliteVectors:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def replace(
        self, item_id: str, model_id: str, acl: Acl, vectors: Sequence[NDArray[np.float32]]
    ) -> None:
        mats = [unit(v) for v in vectors]  # validate everything before touching the database

        def tx(conn: sqlite3.Connection) -> None:
            if mats:
                dim = _model_dim(conn, model_id)
                if dim is None:
                    dim = int(mats[0].shape[0])
                    conn.execute(
                        "INSERT INTO vec_models (model_id, dim) VALUES (?, ?)", (model_id, dim)
                    )
                if any(m.shape[0] != dim for m in mats):
                    raise ValidationError(f"model {model_id} has dimension {dim}")
            conn.execute(
                "DELETE FROM vec_chunks WHERE item_id = ? AND model_id = ?", (item_id, model_id)
            )
            conn.executemany(
                "INSERT INTO vec_chunks (item_id, model_id, ord, org_id, scope_level, scope_owner, "
                "visibility, vec) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (item_id, model_id, i, *acl_params(acl), m.astype("<f4").tobytes())
                    for i, m in enumerate(mats)
                ],
            )

        await self._db.run(tx, write=True)

    async def search(
        self, flt: ScopeFilter, model_id: str, vector: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        q = unit(vector)
        where, params = acl_where(flt)

        def tx(conn: sqlite3.Connection) -> list[Hit]:
            dim = _model_dim(conn, model_id)
            if dim is None or limit <= 0:
                return []
            if q.shape[0] != dim:
                raise ValidationError(f"model {model_id} has dimension {dim}, got {q.shape[0]}")
            cur = conn.cursor()
            cur.row_factory = (
                None  # plain tuples: a scan reads every row of the access-filtered set
            )
            cur.execute(
                f"SELECT item_id, ord, vec FROM vec_chunks WHERE model_id = ? AND {where}",  # noqa: S608
                [model_id, *params],
            )
            best: dict[str, tuple[float, int]] = {}
            # A chunk scoring below the `limit`-th best item so far can never reach the result
            # (the floor only rises), so only chunks at or above it need Python-level work. Equal
            # scores stay in: they are ordered by item id below.
            floor = -np.inf
            while rows := cur.fetchmany(BATCH):
                mat = np.frombuffer(b"".join([r[2] for r in rows]), dtype="<f4").reshape(
                    len(rows), dim
                )
                sims = mat @ q
                for i in np.flatnonzero(sims >= floor).tolist():
                    item_id, ordinal, sim = rows[i][0], rows[i][1], float(sims[i])
                    if item_id not in best or sim > best[item_id][0]:
                        best[item_id] = (sim, ordinal)
                if len(best) >= limit:
                    floor = sorted((v[0] for v in best.values()), reverse=True)[limit - 1]
            ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
            return [Hit(item, score, ordinal) for item, (score, ordinal) in ranked]

        return await self._db.run(tx)

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE vec_chunks SET org_id=?, scope_level=?, scope_owner=?, visibility=? WHERE item_id=?",
                (*acl_params(acl), item_id),
            )

        await self._db.run(tx, write=True)

    async def model_stats(self) -> dict[str, int]:
        def tx(conn: sqlite3.Connection) -> dict[str, int]:
            stats = dict.fromkeys(
                (r[0] for r in conn.execute("SELECT model_id FROM vec_models")), 0
            )
            for r in conn.execute(
                "SELECT model_id, COUNT(DISTINCT item_id) FROM vec_chunks GROUP BY model_id"
            ):
                stats[r[0]] = int(r[1])
            return stats

        return await self._db.run(tx)

    async def drop_model(self, model_id: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(DISTINCT item_id) FROM vec_chunks WHERE model_id = ?", (model_id,)
            ).fetchone()
            conn.execute("DELETE FROM vec_chunks WHERE model_id = ?", (model_id,))
            conn.execute("DELETE FROM vec_models WHERE model_id = ?", (model_id,))
            return int(row[0])

        return await self._db.run(tx, write=True)

    async def delete(self, item_id: str) -> None:
        await self._db.run(
            lambda c: c.execute("DELETE FROM vec_chunks WHERE item_id = ?", (item_id,)), write=True
        )
