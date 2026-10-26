"""SQLite PromotionRepository."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.domain.errors import Conflict, ValidationError
from hive_mcp.domain.promotion import TRANSITIONS, Promotion, PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.adapters.sqlite.db import Database

COLS = (
    "id, org_id, item_id, requester_id, scope_level, scope_owner, visibility, item_revision, "
    "reason, status, created_at, decided_by, decided_at, note"
)


def _row(r: sqlite3.Row) -> Promotion:
    return Promotion(
        id=r["id"],
        org_id=r["org_id"],
        item_id=r["item_id"],
        requester_id=r["requester_id"],
        scope=Scope(ScopeLevel(r["scope_level"]), r["scope_owner"]),
        visibility=Visibility(r["visibility"]),
        item_revision=r["item_revision"],
        created_at=parse_ts(r["created_at"]),
        reason=r["reason"],
        status=PromotionStatus(r["status"]),
        decided_by=r["decided_by"],
        decided_at=parse_ts(r["decided_at"]) if r["decided_at"] else None,
        note=r["note"],
    )


class SqlitePromotions:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, promotion: Promotion) -> Promotion:
        def tx(conn: sqlite3.Connection) -> Promotion:
            try:
                conn.execute(
                    f"INSERT INTO promotions ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
                    (
                        promotion.id,
                        promotion.org_id,
                        promotion.item_id,
                        promotion.requester_id,
                        int(promotion.scope.level),
                        promotion.scope.owner,
                        str(promotion.visibility),
                        promotion.item_revision,
                        promotion.reason,
                        str(promotion.status),
                        ts(promotion.created_at),
                        promotion.decided_by,
                        ts(promotion.decided_at) if promotion.decided_at else None,
                        promotion.note,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    "duplicate promotion id or item already has a pending request"
                ) from exc
            return promotion

        return await self._db.run(tx, write=True)

    async def get(self, org_id: str, promotion_id: str) -> Promotion | None:
        def tx(conn: sqlite3.Connection) -> Promotion | None:
            r = conn.execute(
                f"SELECT {COLS} FROM promotions WHERE id = ? AND org_id = ?",  # noqa: S608
                (promotion_id, org_id),
            ).fetchone()
            return _row(r) if r else None

        return await self._db.run(tx)

    async def transition(  # noqa: PLR0913
        self,
        org_id: str,
        promotion_id: str,
        expected: PromotionStatus,
        to: PromotionStatus,
        *,
        by: str,
        note: str,
        at: datetime,
    ) -> Promotion | None:
        if to not in TRANSITIONS.get(expected, frozenset()):
            raise ValidationError(f"illegal transition {expected} -> {to}")

        def tx(conn: sqlite3.Connection) -> Promotion | None:
            cur = conn.execute(
                "UPDATE promotions SET status = ?, decided_by = ?, decided_at = ?, note = ? "
                "WHERE id = ? AND org_id = ? AND status = ?",
                (str(to), by, ts(at), note, promotion_id, org_id, str(expected)),
            )
            if cur.rowcount != 1:
                return None
            r = conn.execute(
                f"SELECT {COLS} FROM promotions WHERE id = ?",  # noqa: S608
                (promotion_id,),
            ).fetchone()
            return _row(r)

        return await self._db.run(tx, write=True)

    async def delete_for_item(self, org_id: str, item_id: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM promotions WHERE org_id = ? AND item_id = ?", (org_id, item_id)
            )
            return cur.rowcount

        return await self._db.run(tx, write=True)

    async def list(
        self,
        org_id: str,
        *,
        status: PromotionStatus | None = None,
        requester_id: str | None = None,
        scopes: frozenset[Scope] | None = None,
        limit: int = 100,
    ) -> list[Promotion]:
        if scopes is not None and not scopes:
            return []

        def tx(conn: sqlite3.Connection) -> list[Promotion]:
            sql = f"SELECT {COLS} FROM promotions WHERE org_id = ?"  # noqa: S608
            params: list[object] = [org_id]
            if status is not None:
                sql, params = sql + " AND status = ?", [*params, str(status)]
            if requester_id is not None:
                sql, params = sql + " AND requester_id = ?", [*params, requester_id]
            if scopes is not None:
                pairs = sorted((int(s.level), s.owner) for s in scopes)
                sql += f" AND (scope_level, scope_owner) IN (VALUES {','.join('(?,?)' for _ in pairs)})"
                params = [*params, *(v for pair in pairs for v in pair)]
            rows = conn.execute(sql + " ORDER BY created_at, id LIMIT ?", [*params, limit])
            return [_row(r) for r in rows.fetchall()]

        return await self._db.run(tx)


__all__ = ["SqlitePromotions"]
