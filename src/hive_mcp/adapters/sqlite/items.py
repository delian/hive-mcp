"""SQLite ItemRepository. Every write is one IMMEDIATE transaction, so checks and updates cannot interleave."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import item_params, ts
from hive_mcp.adapters.sqlite.item_queries import fetch_item, fetch_view, list_page, load_tags
from hive_mcp.domain import access
from hive_mcp.domain.errors import (
    Conflict,
    HiveError,
    IdempotencyConflict,
    NotFound,
    PermissionDenied,
)
from hive_mcp.domain.item import apply_patch
from hive_mcp.ports.items import CreateResult

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item, ItemPatch, ItemQuery, ItemView, Page
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility

_INSERT = (
    "INSERT INTO items (id, org_id, kind, title, body, scope_level, scope_owner, visibility, status, "
    "author_id, created_at, updated_at, revision, supersedes, source, content_hash) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _sync_tags(conn: sqlite3.Connection, before: Item, after: Item) -> None:
    for tag in before.tags - after.tags:
        conn.execute("DELETE FROM item_tags WHERE item_id = ? AND tag = ?", (after.id, tag))
    for tag in after.tags - before.tags:
        conn.execute("INSERT INTO item_tags (item_id, tag) VALUES (?, ?)", (after.id, tag))


def _store_update(conn: sqlite3.Connection, before: Item, after: Item) -> None:
    cur = conn.execute(
        "UPDATE items SET kind=?, title=?, body=?, status=?, scope_level=?, scope_owner=?, visibility=?, "
        "updated_at=?, revision=?, content_hash=? WHERE id=? AND revision=?",
        (
            str(after.kind),
            after.title,
            after.body,
            str(after.status),
            int(after.scope.level),
            after.scope.owner,
            str(after.visibility),
            ts(after.updated_at),
            after.revision,
            after.content_hash,
            after.id,
            before.revision,
        ),
    )
    if cur.rowcount != 1:
        raise Conflict(f"{after.id} changed concurrently")
    _sync_tags(conn, before, after)


class SqliteItems:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def create(
        self, principal: Principal, item: Item, idempotency_key: str | None = None
    ) -> CreateResult:
        if item.author_id != principal.id or not access.can_create(principal, item.acl):
            raise PermissionDenied("cannot create in this scope")

        def tx(conn: sqlite3.Connection) -> CreateResult:
            if idempotency_key:
                row = conn.execute(
                    "SELECT fingerprint, item_id FROM idempotency WHERE org_id=? AND author_id=? AND key=?",
                    (principal.org_id, principal.id, idempotency_key),
                ).fetchone()
                if row:
                    if row["fingerprint"] != item.fingerprint():
                        raise IdempotencyConflict(idempotency_key)
                    existing = fetch_item(conn, row["item_id"])
                    if existing is None:
                        raise HiveError("idempotency record points at a missing item")
                    return CreateResult(existing, created=False)
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item.id,)).fetchone():
                raise Conflict(f"id exists: {item.id}")
            conn.execute(_INSERT, item_params(item))
            conn.executemany(
                "INSERT INTO item_tags (item_id, tag) VALUES (?, ?)",
                [(item.id, t) for t in item.tags],
            )
            if idempotency_key:
                conn.execute(
                    "INSERT INTO idempotency (org_id, author_id, key, fingerprint, item_id) VALUES (?,?,?,?,?)",
                    (principal.org_id, principal.id, idempotency_key, item.fingerprint(), item.id),
                )
            return CreateResult(item, created=True)

        return await self._db.run(tx, write=True)

    async def get(self, principal: Principal, item_id: str) -> ItemView:
        flt = access.read_filter(principal)

        def tx(conn: sqlite3.Connection) -> ItemView:
            view = fetch_view(conn, flt, item_id)
            if view is None:
                raise NotFound(item_id)
            return view

        return await self._db.run(tx)

    async def get_many(self, principal: Principal, item_ids: Sequence[str]) -> list[ItemView]:
        flt = access.read_filter(principal)

        def tx(conn: sqlite3.Connection) -> list[ItemView]:
            views = (fetch_view(conn, flt, i) for i in item_ids)
            return [v for v in views if v is not None]

        return await self._db.run(tx)

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        flt = access.read_filter(principal)

        def tx(conn: sqlite3.Connection) -> Item:
            view = fetch_view(conn, flt, item_id)
            if view is None:
                raise NotFound(item_id)
            if not access.can_write(principal, view.item.acl, view.item.author_id):
                raise PermissionDenied(item_id)
            if view.item.revision != expected_revision:
                raise Conflict(f"revision {view.item.revision} != expected {expected_revision}")
            updated = apply_patch(view.item, patch, self._clock.now())
            _store_update(conn, view.item, updated)
            return updated

        return await self._db.run(tx, write=True)

    async def relocate(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        flt = access.read_filter(principal)

        def tx(conn: sqlite3.Connection) -> Item:
            view = fetch_view(conn, flt, item_id)
            if view is None:
                raise NotFound(item_id)
            target = replace(view.item, scope=scope, visibility=visibility)  # validates invariants
            if not access.can_write(principal, view.item.acl, view.item.author_id):
                raise PermissionDenied(item_id)
            if not access.can_create(principal, target.acl):
                raise PermissionDenied("not a member of the target scope")
            if view.item.revision != expected_revision:
                raise Conflict(f"revision {view.item.revision} != expected {expected_revision}")
            moved = replace(target, revision=target.revision + 1, updated_at=self._clock.now())
            _store_update(conn, view.item, moved)
            return moved

        return await self._db.run(tx, write=True)

    async def list(self, principal: Principal, query: ItemQuery) -> Page:
        return await self._db.run(lambda conn: list_page(conn, principal, query))


__all__ = ["SqliteItems", "load_tags"]
