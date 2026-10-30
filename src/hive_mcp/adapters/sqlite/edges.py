"""SQLite EdgeRepository."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.domain.edge import Edge

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from hive_mcp.adapters.sqlite.db import Database

COLS = "org_id, src, dst, relation, author_id, created_at"


def _edge(r: sqlite3.Row) -> Edge:
    return Edge(
        r["org_id"], r["src"], r["dst"], r["relation"], r["author_id"], parse_ts(r["created_at"])
    )


class SqliteEdges:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, edge: Edge) -> bool:
        def tx(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO edges ({COLS}) VALUES (?,?,?,?,?,?)",  # noqa: S608
                (edge.org_id, *edge.key, edge.author_id, ts(edge.created_at)),
            )
            return cur.rowcount == 1

        return await self._db.run(tx, write=True)

    async def get(self, org_id: str, src: str, dst: str, relation: str) -> Edge | None:
        def tx(conn: sqlite3.Connection) -> Edge | None:
            r = conn.execute(
                f"SELECT {COLS} FROM edges WHERE org_id=? AND src=? AND dst=? AND relation=?",  # noqa: S608
                (org_id, src, dst, relation),
            ).fetchone()
            return _edge(r) if r else None

        return await self._db.run(tx)

    async def remove(self, org_id: str, src: str, dst: str, relation: str) -> bool:
        def tx(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM edges WHERE org_id=? AND src=? AND dst=? AND relation=?",
                (org_id, src, dst, relation),
            )
            return cur.rowcount == 1

        return await self._db.run(tx, write=True)

    async def pseudonymise_person(self, org_id: str, person_id: str, alias: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE edges SET author_id = ? WHERE org_id = ? AND author_id = ?",
                (alias, org_id, person_id),
            )
            return cur.rowcount

        return await self._db.run(tx, write=True)

    async def remove_item(self, org_id: str, item_id: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM edges WHERE org_id = ? AND (src = ? OR dst = ?)",
                (org_id, item_id, item_id),
            )
            return cur.rowcount

        return await self._db.run(tx, write=True)

    async def list_page(
        self, org_id: str, *, after: tuple[str, str, str] | None = None, limit: int = 500
    ) -> list[Edge]:
        def tx(conn: sqlite3.Connection) -> list[Edge]:
            sql = f"SELECT {COLS} FROM edges WHERE org_id = ?"  # noqa: S608
            params: list[object] = [org_id]
            if after is not None:
                sql += " AND (src, dst, relation) > (?, ?, ?)"
                params += after
            rows = conn.execute(sql + " ORDER BY src, dst, relation LIMIT ?", [*params, limit])
            return [_edge(r) for r in rows.fetchall()]

        return await self._db.run(tx)

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

        def tx(conn: sqlite3.Connection) -> list[Edge]:
            marks = ",".join("?" * len(item_ids))
            sql = f"SELECT {COLS} FROM edges WHERE org_id = ? AND (src IN ({marks}) OR dst IN ({marks}))"  # noqa: S608
            params: list[object] = [org_id, *item_ids, *item_ids]
            if relations:
                sql += f" AND relation IN ({','.join('?' * len(relations))})"
                params += sorted(relations)
            rows = conn.execute(
                sql + " ORDER BY created_at, src, dst, relation LIMIT ?", [*params, limit]
            )
            return [_edge(r) for r in rows.fetchall()]

        return await self._db.run(tx)
