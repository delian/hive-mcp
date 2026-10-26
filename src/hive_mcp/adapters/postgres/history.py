"""PostgreSQL item history: previous versions are copied into item_revisions by every update/move."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.adapters.postgres.item_queries import fetch_view
from hive_mcp.domain import access
from hive_mcp.domain.errors import NotFound
from hive_mcp.domain.history import KEEP_VERSIONS, ItemVersion, current_version, readable
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from datetime import datetime

    import psycopg

    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal

COLS = (
    "item_id, org_id, revision, title, body, kind, status, tags, scope_level, scope_owner, "
    "visibility, item_author, written_at, replaced_at, replaced_by"
)


async def record_version(
    conn: psycopg.AsyncConnection[Any], before: Item, actor: str, at: datetime
) -> None:
    """Called inside the update/move transaction with the state that is about to be replaced."""
    await conn.execute(
        f"INSERT INTO item_revisions ({COLS}) VALUES ({', '.join(['%s'] * 15)})",  # noqa: S608
        (
            before.id, before.org_id, before.revision, before.title, before.body, str(before.kind),
            str(before.status), sorted(before.tags), int(before.scope.level), before.scope.owner,
            str(before.visibility), before.author_id, before.updated_at, at, actor,
        ),
    )  # fmt: skip
    await conn.execute(
        "DELETE FROM item_revisions WHERE item_id = %s AND revision NOT IN "
        "(SELECT revision FROM item_revisions WHERE item_id = %s ORDER BY revision DESC LIMIT %s)",
        (before.id, before.id, KEEP_VERSIONS),
    )


def _version(r: dict[str, Any]) -> ItemVersion:
    return ItemVersion(
        r["item_id"], r["org_id"], r["revision"], r["title"], r["body"], Kind(r["kind"]),
        Status(r["status"]), frozenset(r["tags"]),
        Scope(ScopeLevel(r["scope_level"]), r["scope_owner"]), Visibility(r["visibility"]),
        r["item_author"], r["written_at"], r["replaced_at"], r["replaced_by"],
    )  # fmt: skip


class PgHistory:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def versions(
        self, principal: Principal, item_id: str, limit: int = 50
    ) -> list[ItemVersion]:
        flt = access.read_filter(principal)
        async with self._db.tx() as conn:
            view = await fetch_view(conn, flt, item_id)
            if view is None:
                raise NotFound(item_id)
            cur = await conn.execute(
                f"SELECT {COLS} FROM item_revisions WHERE item_id = %s ORDER BY revision DESC",  # noqa: S608
                (item_id,),
            )
            rows = await cur.fetchall()
        return readable(flt, [current_version(view.item), *(_version(r) for r in rows)])[:limit]

    async def version(self, principal: Principal, item_id: str, revision: int) -> ItemVersion:
        for v in await self.versions(principal, item_id, limit=KEEP_VERSIONS + 1):
            if v.revision == revision:
                return v
        raise NotFound(f"{item_id}@{revision}")
