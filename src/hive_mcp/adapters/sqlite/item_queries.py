"""Read-side SQL for items: single fetch, tag loading and the paginated list."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.acl import acl_where, member_where
from hive_mcp.adapters.sqlite.codec import ITEM_COLS, row_to_item, ts
from hive_mcp.domain.access import read_filter
from hive_mcp.domain.item import Cursor, Item, ItemView, Page

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from hive_mcp.domain.access import ScopeFilter
    from hive_mcp.domain.item import ItemQuery
    from hive_mcp.domain.principal import Principal

_CHUNK = 500  # stay far below SQLite's bound-variable limit


def load_tags(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, frozenset[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for lo in range(0, len(ids), _CHUNK):
        part = ids[lo : lo + _CHUNK]
        marks = ",".join("?" * len(part))
        for row in conn.execute(
            f"SELECT item_id, tag FROM item_tags WHERE item_id IN ({marks})",  # noqa: S608
            part,
        ):
            found[row["item_id"]].add(row["tag"])
    return {i: frozenset(found.get(i, ())) for i in ids}


def fetch_item(conn: sqlite3.Connection, item_id: str) -> Item | None:
    row = conn.execute(f"SELECT {ITEM_COLS} FROM items WHERE id = ?", (item_id,)).fetchone()  # noqa: S608
    return row_to_item(row, load_tags(conn, [item_id])[item_id]) if row else None


def fetch_view(conn: sqlite3.Connection, flt: ScopeFilter, item_id: str) -> ItemView | None:
    """None for missing *and* invisible items (callers raise NotFound for both)."""
    item = fetch_item(conn, item_id)
    tier = flt.tier(item.acl) if item else None
    return ItemView(item, tier) if item and tier else None


def list_page(conn: sqlite3.Connection, principal: Principal, query: ItemQuery) -> Page:
    flt = read_filter(principal)
    if not query.statuses:
        return Page((), None)
    where, params = acl_where(flt, "i")
    conds = [where, f"i.status IN ({','.join('?' * len(query.statuses))})"]
    params += sorted(str(s) for s in query.statuses)
    if query.kinds:
        conds.append(f"i.kind IN ({','.join('?' * len(query.kinds))})")
        params += sorted(str(k) for k in query.kinds)
    for tag in sorted(query.tags_all):
        conds.append("EXISTS (SELECT 1 FROM item_tags t WHERE t.item_id = i.id AND t.tag = ?)")
        params.append(tag)
    if not query.include_referenced:
        member, member_params = member_where(flt, "i")
        conds.append(member)
        params += member_params
    column = "i.updated_at" if query.by_updated else "i.created_at"
    if query.after:
        stamp = ts(query.after.created_at)
        conds.append(f"({column} < ? OR ({column} = ? AND i.id < ?))")
        params += [stamp, stamp, query.after.id]
    sql = (
        # only "?" marks and constant column names are interpolated; every value is bound
        f"SELECT {', '.join('i.' + c.strip() for c in ITEM_COLS.split(','))} FROM items i "  # noqa: S608
        f"WHERE {' AND '.join(conds)} ORDER BY {column} DESC, i.id DESC LIMIT ?"
    )
    rows = conn.execute(sql, [*params, query.limit + 1]).fetchall()
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    tags = load_tags(conn, [r["id"] for r in rows])
    views = []
    for row in rows:
        item = row_to_item(row, tags[row["id"]])
        tier = flt.tier(item.acl)
        if tier is not None:  # always true; the tier is still decided in one place
            views.append(ItemView(item, tier))
    last = views[-1].item if views else None
    when = (last.updated_at if query.by_updated else last.created_at) if last else None
    return Page(tuple(views), Cursor(when, last.id).encode() if more and last and when else None)
