"""SQLite ItemMaintenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import ITEM_COLS, parse_ts, row_to_item, ts
from hive_mcp.adapters.sqlite.item_queries import fetch_item, load_tags
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.ports.maintenance import AuthorUsage

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.item import Item
    from hive_mcp.ports.maintenance import ItemFilter


class SqliteMaintenance:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def purge_system(self, org_id: str, item_id: str) -> Item | None:
        def tx(conn: sqlite3.Connection) -> Item | None:
            item = fetch_item(conn, item_id)
            if item is None or item.org_id != org_id:
                return None
            conn.execute(
                "DELETE FROM idempotency WHERE org_id = ? AND item_id = ?", (org_id, item_id)
            )
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))  # tags cascade
            return item

        return await self._db.run(tx, write=True)

    async def find_system(
        self, flt: ItemFilter, *, after_id: str | None = None, limit: int = 100
    ) -> list[Item]:
        conds: list[str] = ["id > ?"]
        params: list[object] = [after_id or ""]
        if flt.org_id is not None:
            conds.append("org_id = ?")
            params.append(flt.org_id)
        if flt.author_id is not None:
            conds.append("author_id = ?")
            params.append(flt.author_id)
        if flt.kinds:
            conds.append(f"kind IN ({','.join('?' * len(flt.kinds))})")
            params += sorted(str(k) for k in flt.kinds)
        if flt.created_before is not None:
            conds.append("created_at < ?")
            params.append(ts(flt.created_before))
        if flt.private_only:
            conds.append("scope_level = ?")
            params.append(int(ScopeLevel.PRIVATE))
        if flt.scope_level is not None:
            conds.append("scope_level = ?")
            params.append(int(flt.scope_level))
        if flt.updated_before is not None:
            conds.append("updated_at < ?")
            params.append(ts(flt.updated_before))

        def tx(conn: sqlite3.Connection) -> list[Item]:
            rows = conn.execute(
                f"SELECT {ITEM_COLS} FROM items WHERE {' AND '.join(conds)} ORDER BY id LIMIT ?",  # noqa: S608
                [*params, limit],
            ).fetchall()
            tags = load_tags(conn, [r["id"] for r in rows])
            return [row_to_item(r, tags[r["id"]]) for r in rows]

        return await self._db.run(tx)

    async def usage(self, org_id: str, author_id: str | None = None) -> list[AuthorUsage]:
        def tx(conn: sqlite3.Connection) -> list[AuthorUsage]:
            rows = conn.execute(
                "SELECT author_id, COUNT(*) AS n, SUM(LENGTH(title) + LENGTH(body)) AS c, "
                "MAX(updated_at) AS last FROM items WHERE org_id = ? AND status != 'archived' "
                "AND (? IS NULL OR author_id = ?) GROUP BY author_id ORDER BY c DESC, author_id",
                (org_id, author_id, author_id),
            ).fetchall()
            return [AuthorUsage(r["author_id"], r["n"], r["c"], parse_ts(r["last"])) for r in rows]

        return await self._db.run(tx)

    async def count_system(
        self, org_id: str | None = None, *, include_archived: bool = False
    ) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM items WHERE (? IS NULL OR org_id = ?) "
                "AND (? OR status != 'archived')",
                (org_id, org_id, include_archived),
            ).fetchone()
            return int(row[0])

        return await self._db.run(tx)

    async def count_by_source(self, org_id: str, source: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM items WHERE org_id = ? AND source = ?", (org_id, source)
            ).fetchone()
            return int(row[0])

        return await self._db.run(tx)
