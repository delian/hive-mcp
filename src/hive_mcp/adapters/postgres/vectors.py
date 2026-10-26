"""pgvector VectorIndex: one halfvec table (with an HNSW index) per embedding model.

pgvector indexes need a fixed dimension, hence a table per model; table names are a hash of the model id,
so they are always safe identifiers. Scores are inner products of unit vectors stored as float16, so they
are cosine similarities accurate to about 1e-3.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from psycopg import sql

from hive_mcp.adapters.postgres.acl import acl_where
from hive_mcp.adapters.postgres.codec import acl_params
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Hit
from hive_mcp.search.vectors import unit

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    import psycopg
    from numpy.typing import NDArray

    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.access import Acl, ScopeFilter

MAX_DIM = 4000  # pgvector's limit for HNSW on halfvec
HNSW_M, HNSW_EF_CONSTRUCTION = 32, 200  # tuned in the Phase 0 benchmark
OVERSAMPLE, MAX_ROUNDS = 4, 4


def table_for(model_id: str) -> str:
    return "vec_" + hashlib.sha256(model_id.encode()).hexdigest()[:16]


def literal(vector: NDArray[np.float32]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in vector.tolist()) + "]"


async def _registry(conn: psycopg.AsyncConnection[Any], model_id: str) -> tuple[int, str] | None:
    cur = await conn.execute(
        "SELECT dim, table_name FROM vec_models WHERE model_id = %s", (model_id,)
    )
    row = await cur.fetchone()
    return (int(row["dim"]), str(row["table_name"])) if row else None


async def _ensure_table(conn: psycopg.AsyncConnection[Any], model_id: str, dim: int) -> str:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"vecmodel:{model_id}",)
    )
    known = await _registry(conn, model_id)
    if known:
        if known[0] != dim:
            raise ValidationError(f"model {model_id} has dimension {known[0]}, got {dim}")
        return known[1]
    if not 1 <= dim <= MAX_DIM:
        raise ValidationError(f"dimension must be between 1 and {MAX_DIM}")
    name = table_for(model_id)
    ident = sql.Identifier(name)
    await conn.execute(
        sql.SQL(
            "CREATE TABLE {t} (item_id text NOT NULL, ord integer NOT NULL, org_id text NOT NULL, "
            "scope_level smallint NOT NULL, scope_owner text NOT NULL, visibility text NOT NULL, "
            "embedding halfvec({d}) NOT NULL, PRIMARY KEY (item_id, ord))"
        ).format(t=ident, d=sql.SQL(str(int(dim))))
    )
    await conn.execute(
        sql.SQL(
            "CREATE INDEX {i} ON {t} USING hnsw (embedding halfvec_ip_ops) "
            "WITH (m = {m}, ef_construction = {e})"
        ).format(
            i=sql.Identifier(name + "_hnsw"),
            t=ident,
            m=sql.SQL(str(HNSW_M)),
            e=sql.SQL(str(HNSW_EF_CONSTRUCTION)),
        )
    )
    await conn.execute(
        sql.SQL("CREATE INDEX {i} ON {t} (org_id, scope_level, scope_owner)").format(
            i=sql.Identifier(name + "_acl"), t=ident
        )
    )
    await conn.execute(
        "INSERT INTO vec_models (model_id, dim, table_name) VALUES (%s, %s, %s)",
        (model_id, dim, name),
    )
    return name


class PgVectors:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def replace(
        self, item_id: str, model_id: str, acl: Acl, vectors: Sequence[NDArray[np.float32]]
    ) -> None:
        mats = [unit(v) for v in vectors]  # validate everything before touching the database
        if mats and len({m.shape[0] for m in mats}) != 1:
            raise ValidationError("all chunk vectors must have the same dimension")
        async with self._db.tx() as conn:
            if mats:
                name = await _ensure_table(conn, model_id, int(mats[0].shape[0]))
            else:
                known = await _registry(conn, model_id)
                if known is None:
                    return
                name = known[1]
            ident = sql.Identifier(name)
            await conn.execute(
                sql.SQL("DELETE FROM {t} WHERE item_id = %s").format(t=ident), (item_id,)
            )
            if mats:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        sql.SQL(
                            "INSERT INTO {t} (item_id, ord, org_id, scope_level, scope_owner, visibility, "
                            "embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::halfvec)"
                        ).format(t=ident),
                        [(item_id, i, *acl_params(acl), literal(m)) for i, m in enumerate(mats)],
                    )

    async def search(
        self, flt: ScopeFilter, model_id: str, vector: NDArray[np.float32], limit: int
    ) -> list[Hit]:
        q = unit(vector)
        if limit < 1:
            return []
        where, params = acl_where(flt)
        async with self._db.tx() as conn:
            known = await _registry(conn, model_id)
            if known is None:
                return []
            if known[0] != q.shape[0]:
                raise ValidationError(
                    f"model {model_id} has dimension {known[0]}, got {q.shape[0]}"
                )
            ident, lit = sql.Identifier(known[1]), literal(q)
            fetch = max(limit * OVERSAMPLE, 40)
            best: dict[str, tuple[float, int]] = {}
            for _ in range(MAX_ROUNDS):
                await conn.execute(f"SET LOCAL hnsw.ef_search = {min(max(fetch, 100), 1000)}")
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                cur = await conn.execute(
                    sql.SQL(  # `where` is built from constants and placeholders only
                        "SELECT item_id, ord, embedding <#> %s::halfvec AS dist FROM {t} WHERE "  # noqa: S608
                        + where
                        + " ORDER BY embedding <#> %s::halfvec LIMIT %s"
                    ).format(t=ident),
                    [lit, *params, lit, fetch],
                )
                rows = await cur.fetchall()
                best = {}
                for row in rows:  # rows arrive best first; keep each item's best chunk
                    best.setdefault(row["item_id"], (-float(row["dist"]), int(row["ord"])))
                if len(best) >= limit or len(rows) < fetch:
                    break
                fetch *= OVERSAMPLE  # chunks of a few items crowded the top; look deeper
        hits = [Hit(item, score, ordinal) for item, (score, ordinal) in best.items()]
        hits.sort(key=lambda h: (-h.score, h.item_id))
        return hits[:limit]

    async def _tables(self, conn: psycopg.AsyncConnection[Any]) -> list[str]:
        cur = await conn.execute("SELECT table_name FROM vec_models")
        return [str(r["table_name"]) for r in await cur.fetchall()]

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        async with self._db.tx() as conn:
            for name in await self._tables(conn):
                await conn.execute(
                    sql.SQL(
                        "UPDATE {t} SET org_id=%s, scope_level=%s, scope_owner=%s, visibility=%s "
                        "WHERE item_id=%s"
                    ).format(t=sql.Identifier(name)),
                    (*acl_params(acl), item_id),
                )

    async def delete(self, item_id: str) -> None:
        async with self._db.tx() as conn:
            for name in await self._tables(conn):
                await conn.execute(
                    sql.SQL("DELETE FROM {t} WHERE item_id = %s").format(t=sql.Identifier(name)),
                    (item_id,),
                )

    async def model_stats(self) -> dict[str, int]:
        async with self._db.tx() as conn:
            cur = await conn.execute("SELECT model_id, table_name FROM vec_models")
            stats = {}
            for row in await cur.fetchall():
                counted = await conn.execute(
                    sql.SQL("SELECT count(DISTINCT item_id) AS n FROM {t}").format(
                        t=sql.Identifier(row["table_name"])
                    )
                )
                stats[str(row["model_id"])] = int((await counted.fetchone() or {"n": 0})["n"])
            return stats

    async def drop_model(self, model_id: str) -> int:
        async with self._db.tx() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"vecmodel:{model_id}",)
            )
            known = await _registry(conn, model_id)
            if known is None:
                return 0
            ident = sql.Identifier(known[1])
            counted = await conn.execute(
                sql.SQL("SELECT count(DISTINCT item_id) AS n FROM {t}").format(t=ident)
            )
            n = int((await counted.fetchone() or {"n": 0})["n"])
            await conn.execute(sql.SQL("DROP TABLE {t}").format(t=ident))
            await conn.execute("DELETE FROM vec_models WHERE model_id = %s", (model_id,))
            return n
