"""Read-side SQL for items: visible single fetch (optionally locking) and the keyset-paginated list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.adapters.postgres.acl import acl_where, member_where
from hive_mcp.adapters.postgres.codec import ITEM_COLS, row_to_item
from hive_mcp.domain.access import read_filter
from hive_mcp.domain.item import Cursor, Item, ItemView, Page

if TYPE_CHECKING:
    import psycopg

    from hive_mcp.domain.access import ScopeFilter
    from hive_mcp.domain.item import ItemQuery
    from hive_mcp.domain.principal import Principal


async def fetch_item(conn: psycopg.AsyncConnection[Any], item_id: str) -> Item | None:
    cur = await conn.execute(f"SELECT {ITEM_COLS} FROM items WHERE id = %s", (item_id,))  # noqa: S608
    row = await cur.fetchone()
    return row_to_item(row) if row else None


async def fetch_view(
    conn: psycopg.AsyncConnection[Any], flt: ScopeFilter, item_id: str, *, lock: bool = False
) -> ItemView | None:
    """None for missing *and* invisible items. With `lock`, only rows visible to the caller are locked."""
    where, params = acl_where(flt)
    suffix = " FOR UPDATE" if lock else ""
    cur = await conn.execute(
        f"SELECT {ITEM_COLS} FROM items WHERE id = %s AND {where}{suffix}",  # noqa: S608
        [item_id, *params],
    )
    row = await cur.fetchone()
    if row is None:
        return None
    item = row_to_item(row)
    tier = flt.tier(item.acl)  # the tier is always decided in Python, in one place
    return ItemView(item, tier) if tier else None


async def list_page(
    conn: psycopg.AsyncConnection[Any], principal: Principal, query: ItemQuery
) -> Page:
    flt = read_filter(principal)
    if not query.statuses:
        return Page((), None)
    where, params = acl_where(flt)
    conds = [where, "status = ANY(%s)"]
    params.append(sorted(str(s) for s in query.statuses))
    if query.kinds:
        conds.append("kind = ANY(%s)")
        params.append(sorted(str(k) for k in query.kinds))
    if query.tags_all:
        conds.append("tags @> %s::text[]")
        params.append(sorted(query.tags_all))
    if not query.include_referenced:
        member, member_params = member_where(flt)
        conds.append(member)
        params += member_params
    column = "updated_at" if query.by_updated else "created_at"
    if query.after:
        conds.append(f"({column}, id) < (%s, %s)")
        params += [query.after.created_at, query.after.id]
    sql = (
        f"SELECT {ITEM_COLS} FROM items WHERE {' AND '.join(conds)} "  # noqa: S608
        f"ORDER BY {column} DESC, id DESC LIMIT %s"
    )
    cur = await conn.execute(sql, [*params, query.limit + 1])
    rows = await cur.fetchall()
    more = len(rows) > query.limit
    views = []
    for row in rows[: query.limit]:
        item = row_to_item(row)
        tier = flt.tier(item.acl)
        if tier is not None:
            views.append(ItemView(item, tier))
    last = views[-1].item if views else None
    when = (last.updated_at if query.by_updated else last.created_at) if last else None
    return Page(tuple(views), Cursor(when, last.id).encode() if more and last and when else None)
