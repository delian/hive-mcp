"""PostgreSQL EdgeRepository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.domain.edge import Edge

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.adapters.postgres.db import PgDatabase

COLS = "org_id, src, dst, relation, author_id, created_at"


def _edge(r: dict[str, Any]) -> Edge:
    return Edge(r["org_id"], r["src"], r["dst"], r["relation"], r["author_id"], r["created_at"])


class PgEdges:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def add(self, edge: Edge) -> bool:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"INSERT INTO edges ({COLS}) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",  # noqa: S608
                (edge.org_id, *edge.key, edge.author_id, edge.created_at),
            )
            return cur.rowcount == 1

    async def get(self, org_id: str, src: str, dst: str, relation: str) -> Edge | None:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {COLS} FROM edges WHERE org_id=%s AND src=%s AND dst=%s AND relation=%s",  # noqa: S608
                (org_id, src, dst, relation),
            )
            r = await cur.fetchone()
        return _edge(r) if r else None

    async def remove(self, org_id: str, src: str, dst: str, relation: str) -> bool:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "DELETE FROM edges WHERE org_id=%s AND src=%s AND dst=%s AND relation=%s",
                (org_id, src, dst, relation),
            )
            return cur.rowcount == 1

    async def pseudonymise_person(self, org_id: str, person_id: str, alias: str) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "UPDATE edges SET author_id = %s WHERE org_id = %s AND author_id = %s",
                (alias, org_id, person_id),
            )
            return cur.rowcount

    async def remove_item(self, org_id: str, item_id: str) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "DELETE FROM edges WHERE org_id = %s AND (src = %s OR dst = %s)",
                (org_id, item_id, item_id),
            )
            return cur.rowcount

    async def list_page(
        self, org_id: str, *, after: tuple[str, str, str] | None = None, limit: int = 500
    ) -> list[Edge]:
        key = after or ("", "", "")
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {COLS} FROM edges WHERE org_id = %s "  # noqa: S608
                "AND (%s OR (src, dst, relation) > (%s, %s, %s)) "
                "ORDER BY src, dst, relation LIMIT %s",
                (org_id, after is None, *key, limit),
            )
            return [_edge(r) for r in await cur.fetchall()]

    async def incident(
        self,
        org_id: str,
        item_ids: Sequence[str],
        *,
        relations: frozenset[str] = frozenset(),
        limit: int = 200,
    ) -> list[Edge]:
        if not item_ids:
            return []
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {COLS} FROM edges WHERE org_id = %s "  # noqa: S608
                "AND (src = ANY(%s) OR dst = ANY(%s)) AND (%s OR relation = ANY(%s)) "
                "ORDER BY created_at, src, dst, relation LIMIT %s",
                (org_id, list(item_ids), list(item_ids), not relations, sorted(relations), limit),
            )
            return [_edge(r) for r in await cur.fetchall()]
