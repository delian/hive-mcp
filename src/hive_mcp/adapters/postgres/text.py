"""PostgreSQL TextIndex on tsvector/GIN. Ranking is ts_rank (not BM25): fine for a first version, see docs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.adapters.postgres.acl import acl_where
from hive_mcp.adapters.postgres.codec import acl_params
from hive_mcp.domain.item import Hit
from hive_mcp.search.tokenize import tokenize

if TYPE_CHECKING:
    from hive_mcp.adapters.postgres.db import PgDatabase
    from hive_mcp.domain.access import Acl, ScopeFilter
    from hive_mcp.ports.indexes import TextDoc


def tsquery_expression(terms: list[str]) -> str:
    """OR of quoted lexemes. Terms come from the shared tokenizer (letters/digits only); quoting and
    quote-doubling keep tsquery operators out even if that ever changes."""
    return " | ".join("'" + t.replace("'", "''").replace("\\", "\\\\") + "'" for t in terms)


class PgText:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def upsert(self, doc: TextDoc) -> None:
        tokens = " ".join(tokenize(doc.text))
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO text_docs (item_id, org_id, scope_level, scope_owner, visibility, tsv) "
                "VALUES (%s,%s,%s,%s,%s, to_tsvector('simple', %s)) ON CONFLICT (item_id) DO UPDATE SET "
                "org_id=EXCLUDED.org_id, scope_level=EXCLUDED.scope_level, scope_owner=EXCLUDED.scope_owner, "
                "visibility=EXCLUDED.visibility, tsv=EXCLUDED.tsv",
                (doc.item_id, *acl_params(doc.acl), tokens),
            )

    async def search(self, flt: ScopeFilter, query: str, limit: int) -> list[Hit]:
        terms = sorted(set(tokenize(query)))
        if not terms or limit < 1:
            return []
        where, params = acl_where(flt)
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT item_id, ts_rank(tsv, q) AS score FROM text_docs, "  # noqa: S608
                f"to_tsquery('simple', %s) q WHERE tsv @@ q AND {where} "
                "ORDER BY score DESC, item_id LIMIT %s",
                [tsquery_expression(terms), *params, limit],
            )
            return [Hit(r["item_id"], float(r["score"])) for r in await cur.fetchall()]

    async def update_acl(self, item_id: str, acl: Acl) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "UPDATE text_docs SET org_id=%s, scope_level=%s, scope_owner=%s, visibility=%s "
                "WHERE item_id=%s",
                (*acl_params(acl), item_id),
            )

    async def delete(self, item_id: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute("DELETE FROM text_docs WHERE item_id = %s", (item_id,))
