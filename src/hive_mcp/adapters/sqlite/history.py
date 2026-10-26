"""SQLite item history: previous versions are copied into item_revisions by every update/move."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.adapters.sqlite.item_queries import fetch_view
from hive_mcp.domain import access
from hive_mcp.domain.errors import NotFound
from hive_mcp.domain.history import KEEP_VERSIONS, ItemVersion, current_version, readable
from hive_mcp.domain.item import Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal

COLS = (
    "item_id, org_id, revision, title, body, kind, status, tags, scope_level, scope_owner, "
    "visibility, item_author, written_at, replaced_at, replaced_by"
)


def record_version(conn: sqlite3.Connection, before: Item, actor: str, at: datetime) -> None:
    """Called inside the update/move transaction with the state that is about to be replaced."""
    conn.execute(
        f"INSERT INTO item_revisions ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
        (
            before.id, before.org_id, before.revision, before.title, before.body, str(before.kind),
            str(before.status), json.dumps(sorted(before.tags)), int(before.scope.level),
            before.scope.owner, str(before.visibility), before.author_id, ts(before.updated_at),
            ts(at), actor,
        ),
    )  # fmt: skip
    conn.execute(
        "DELETE FROM item_revisions WHERE item_id = ? AND revision NOT IN "
        "(SELECT revision FROM item_revisions WHERE item_id = ? ORDER BY revision DESC LIMIT ?)",
        (before.id, before.id, KEEP_VERSIONS),
    )


def _version(r: sqlite3.Row) -> ItemVersion:
    return ItemVersion(
        r["item_id"], r["org_id"], r["revision"], r["title"], r["body"], Kind(r["kind"]),
        Status(r["status"]), frozenset(json.loads(r["tags"])),
        Scope(ScopeLevel(r["scope_level"]), r["scope_owner"]), Visibility(r["visibility"]),
        r["item_author"], parse_ts(r["written_at"]), parse_ts(r["replaced_at"]), r["replaced_by"],
    )  # fmt: skip


class SqliteHistory:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def versions(
        self, principal: Principal, item_id: str, limit: int = 50
    ) -> list[ItemVersion]:
        flt = access.read_filter(principal)

        def tx(conn: sqlite3.Connection) -> list[ItemVersion]:
            view = fetch_view(conn, flt, item_id)
            if view is None:
                raise NotFound(item_id)
            rows = conn.execute(
                f"SELECT {COLS} FROM item_revisions WHERE item_id = ? ORDER BY revision DESC",  # noqa: S608
                (item_id,),
            ).fetchall()
            return readable(flt, [current_version(view.item), *(_version(r) for r in rows)])[:limit]

        return await self._db.run(tx)

    async def version(self, principal: Principal, item_id: str, revision: int) -> ItemVersion:
        for v in await self.versions(principal, item_id, limit=KEEP_VERSIONS + 1):
            if v.revision == revision:
                return v
        raise NotFound(f"{item_id}@{revision}")
