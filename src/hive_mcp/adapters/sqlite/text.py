"""SQLite TextIndex on FTS5. Text is tokenised in Python (the shared tokenizer) so all backends match alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.acl import acl_where
from hive_mcp.adapters.sqlite.codec import acl_params
from hive_mcp.domain.item import Hit
from hive_mcp.search.tokenize import query_terms, tokenize

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.domain.access import Acl, ScopeFilter
    from hive_mcp.ports.indexes import TextDoc


def match_expression(terms: list[str]) -> str:
    """OR of quoted terms: quoting stops FTS5 operators (NEAR, NOT, *, :) in user text from being interpreted."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


class SqliteText:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, doc: TextDoc) -> None:
        tokens = " ".join(tokenize(doc.text))

        def tx(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT rowid FROM text_meta WHERE item_id = ?", (doc.item_id,)
            ).fetchone()
            if row:
                rowid = row["rowid"]
                conn.execute(
                    "UPDATE text_meta SET org_id=?, scope_level=?, scope_owner=?, visibility=? WHERE rowid=?",
                    (*acl_params(doc.acl), rowid),
                )
                conn.execute("DELETE FROM text_fts WHERE rowid = ?", (rowid,))
            else:
                cur = conn.execute(
                    "INSERT INTO text_meta (item_id, org_id, scope_level, scope_owner, visibility) VALUES (?,?,?,?,?)",
                    (doc.item_id, *acl_params(doc.acl)),
                )
                rowid = cur.lastrowid
            conn.execute("INSERT INTO text_fts (rowid, tokens) VALUES (?, ?)", (rowid, tokens))

        await self._db.run(tx, write=True)

    async def search(self, flt: ScopeFilter, query: str, limit: int) -> list[Hit]:
        terms = sorted(set(query_terms(query)))
        if not terms or limit < 1:
            return []
        where, params = acl_where(flt, "m")

        def tx(conn: sqlite3.Connection) -> list[Hit]:
            rows = conn.execute(
                "SELECT m.item_id AS item_id, bm25(text_fts) AS score FROM text_fts "  # noqa: S608
                f"JOIN text_meta m ON m.rowid = text_fts.rowid WHERE text_fts MATCH ? AND {where} "
                "ORDER BY score, m.item_id LIMIT ?",
                [match_expression(terms), *params, limit],
            ).fetchall()
            return [Hit(r["item_id"], -float(r["score"])) for r in rows]  # bm25(): lower = better

        return await self._db.run(tx)

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE text_meta SET org_id=?, scope_level=?, scope_owner=?, visibility=? WHERE item_id=?",
                (*acl_params(acl), item_id),
            )

        await self._db.run(tx, write=True)

    async def delete(self, item_id: str) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT rowid FROM text_meta WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row:
                conn.execute("DELETE FROM text_fts WHERE rowid = ?", (row["rowid"],))
                conn.execute("DELETE FROM text_meta WHERE rowid = ?", (row["rowid"],))

        await self._db.run(tx, write=True)
