"""PostgreSQL ItemMaintenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.adapters.postgres.codec import ITEM_COLS, row_to_item
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.ports.maintenance import AuthorUsage

if TYPE_CHECKING:
    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.item import Item
    from hive_mcp.ports.maintenance import ItemFilter


class PgMaintenance:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def purge_system(self, org_id: str, item_id: str) -> Item | None:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"DELETE FROM items WHERE id = %s AND org_id = %s RETURNING {ITEM_COLS}",  # noqa: S608
                (item_id, org_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "DELETE FROM idempotency WHERE org_id = %s AND item_id = %s", (org_id, item_id)
            )
            return row_to_item(row)

    async def find_system(
        self, flt: ItemFilter, *, after_id: str | None = None, limit: int = 100
    ) -> list[Item]:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {ITEM_COLS} FROM items WHERE id > %s "  # noqa: S608
                "AND (%s::text IS NULL OR org_id = %s) "
                "AND (%s::text IS NULL OR author_id = %s) "
                "AND (%s OR kind = ANY(%s)) "
                "AND (%s::timestamptz IS NULL OR created_at < %s) "
                "AND (NOT %s OR scope_level = %s) "
                "AND (%s::smallint IS NULL OR scope_level = %s) "
                "AND (%s::timestamptz IS NULL OR updated_at < %s) ORDER BY id LIMIT %s",
                (
                    after_id or "",
                    flt.org_id, flt.org_id,
                    flt.author_id, flt.author_id,
                    not flt.kinds, sorted(str(k) for k in flt.kinds),
                    flt.created_before, flt.created_before,
                    flt.private_only, int(ScopeLevel.PRIVATE),
                    None if flt.scope_level is None else int(flt.scope_level),
                    None if flt.scope_level is None else int(flt.scope_level),
                    flt.updated_before, flt.updated_before,
                    limit,
                ),
            )  # fmt: skip
            return [row_to_item(r) for r in await cur.fetchall()]

    async def usage(self, org_id: str, author_id: str | None = None) -> list[AuthorUsage]:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT author_id, count(*) AS n, sum(length(title) + length(body)) AS c, "
                "max(updated_at) AS last FROM items WHERE org_id = %s AND status != 'archived' "
                "AND (%s::text IS NULL OR author_id = %s) GROUP BY author_id "
                "ORDER BY c DESC, author_id",
                (org_id, author_id, author_id),
            )
            return [
                AuthorUsage(r["author_id"], int(r["n"]), int(r["c"]), r["last"])
                for r in await cur.fetchall()
            ]

    async def count_system(
        self, org_id: str | None = None, *, include_archived: bool = False
    ) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM items WHERE (%s::text IS NULL OR org_id = %s) "
                "AND (%s OR status != 'archived')",
                (org_id, org_id, include_archived),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def count_by_source(self, org_id: str, source: str) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM items WHERE org_id = %s AND source = %s",
                (org_id, source),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0
