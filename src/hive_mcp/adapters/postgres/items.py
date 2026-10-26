"""PostgreSQL ItemRepository. Writes lock the visible row (FOR UPDATE), so revision checks cannot race."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from hive_mcp.adapters.postgres.acl import acl_where
from hive_mcp.adapters.postgres.codec import ITEM_COLS, item_params, row_to_item
from hive_mcp.adapters.postgres.db import lock
from hive_mcp.adapters.postgres.item_queries import fetch_item, fetch_view, list_page
from hive_mcp.domain import access
from hive_mcp.domain.errors import Conflict, HiveError, IdempotencyConflict, NotFound
from hive_mcp.domain.item import ItemView, apply_patch, relocate_item
from hive_mcp.domain.rules import (
    authorize_create,
    authorize_relocate,
    authorize_update,
    check_revision,
)
from hive_mcp.ports.items import CreateResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item, ItemPatch, ItemQuery, Page
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility

_INSERT = (
    f"INSERT INTO items ({ITEM_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"  # noqa: S608
)
_UPDATE = (
    "UPDATE items SET kind=%s, title=%s, body=%s, status=%s, scope_level=%s, scope_owner=%s, "
    "visibility=%s, updated_at=%s, revision=%s, content_hash=%s, tags=%s WHERE id=%s AND revision=%s"
)


async def _store_update(conn: psycopg.AsyncConnection[Any], before: Item, after: Item) -> None:
    cur = await conn.execute(
        _UPDATE,
        (
            str(after.kind),
            after.title,
            after.body,
            str(after.status),
            int(after.scope.level),
            after.scope.owner,
            str(after.visibility),
            after.updated_at,
            after.revision,
            after.content_hash,
            sorted(after.tags),
            after.id,
            before.revision,
        ),
    )
    if cur.rowcount != 1:
        raise Conflict(f"{after.id} changed concurrently")


class PgItems:
    def __init__(self, db: PgDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def create(
        self, principal: Principal, item: Item, idempotency_key: str | None = None
    ) -> CreateResult:
        authorize_create(principal, item, idempotency_key)
        async with self._db.tx() as conn:
            if idempotency_key:
                await lock(conn, "idem", principal.org_id, principal.id, idempotency_key)
                cur = await conn.execute(
                    "SELECT fingerprint, item_id FROM idempotency WHERE org_id=%s AND author_id=%s AND key=%s",
                    (principal.org_id, principal.id, idempotency_key),
                )
                row = await cur.fetchone()
                if row:
                    if row["fingerprint"] != item.fingerprint():
                        raise IdempotencyConflict(idempotency_key)
                    existing = await fetch_item(conn, row["item_id"])
                    if existing is None:
                        raise HiveError("idempotency record points at a missing item")
                    return CreateResult(existing, created=False)
            try:
                async with (
                    conn.transaction()
                ):  # savepoint: a duplicate id must not poison the outer tx
                    await conn.execute(_INSERT, item_params(item))
            except psycopg.errors.UniqueViolation as exc:
                raise Conflict(f"id exists: {item.id}") from exc
            if idempotency_key:
                await conn.execute(
                    "INSERT INTO idempotency (org_id, author_id, key, fingerprint, item_id) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (principal.org_id, principal.id, idempotency_key, item.fingerprint(), item.id),
                )
            return CreateResult(item, created=True)

    async def get(self, principal: Principal, item_id: str) -> ItemView:
        async with self._db.tx() as conn:
            view = await fetch_view(conn, access.read_filter(principal), item_id)
        if view is None:
            raise NotFound(item_id)
        return view

    async def get_system(self, org_id: str, item_id: str) -> Item | None:
        async with self._db.tx() as conn:
            item = await fetch_item(conn, item_id)
        return item if item and item.org_id == org_id else None

    async def list_ids_system(self, org_id: str, after: str | None, limit: int) -> list[str]:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT id FROM items WHERE org_id = %s AND id > %s ORDER BY id LIMIT %s",
                [org_id, after or "", limit],
            )
            return [r["id"] for r in await cur.fetchall()]

    async def get_many(self, principal: Principal, item_ids: Sequence[str]) -> list[ItemView]:
        flt = access.read_filter(principal)
        where, params = acl_where(flt)
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {ITEM_COLS} FROM items WHERE id = ANY(%s) AND {where}",  # noqa: S608
                [list(item_ids), *params],
            )
            found = {r["id"]: row_to_item(r) for r in await cur.fetchall()}
        views = []
        for item_id in item_ids:
            item = found.get(item_id)
            tier = flt.tier(item.acl) if item else None
            if item and tier:
                views.append(ItemView(item, tier))
        return views

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        async with self._db.tx() as conn:
            view = await fetch_view(conn, access.read_filter(principal), item_id, lock=True)
            if view is None:
                raise NotFound(item_id)
            authorize_update(principal, view.item, expected_revision)
            updated = apply_patch(view.item, patch, self._clock.now())
            await _store_update(conn, view.item, updated)
            return updated

    async def relocate(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        async with self._db.tx() as conn:
            view = await fetch_view(conn, access.read_filter(principal), item_id, lock=True)
            if view is None:
                raise NotFound(item_id)
            moved = relocate_item(view.item, scope, visibility, self._clock.now())
            authorize_relocate(principal, view.item, moved, expected_revision)
            await _store_update(conn, view.item, moved)
            return moved

    async def tag_counts(
        self, principal: Principal, *, prefix: str = "", limit: int = 100
    ) -> list[tuple[str, int]]:
        where, params = acl_where(access.read_filter(principal))
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT tag, count(*) AS n FROM items, unnest(tags) AS tag "  # noqa: S608
                f"WHERE {where} AND status IN ('active', 'draft') AND starts_with(tag, %s) "
                "GROUP BY tag ORDER BY n DESC, tag LIMIT %s",
                [*params, prefix, limit],
            )
            return [(r["tag"], r["n"]) for r in await cur.fetchall()]

    async def relocate_system(
        self,
        org_id: str,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {ITEM_COLS} FROM items WHERE id = %s AND org_id = %s FOR UPDATE",  # noqa: S608
                (item_id, org_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise NotFound(item_id)
            current = row_to_item(row)
            check_revision(current, expected_revision)
            moved = relocate_item(current, scope, visibility, self._clock.now())
            await _store_update(conn, current, moved)
            return moved

    async def list(self, principal: Principal, query: ItemQuery) -> Page:
        async with self._db.tx() as conn:
            return await list_page(conn, principal, query)
