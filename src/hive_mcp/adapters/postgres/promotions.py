"""PostgreSQL PromotionRepository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from hive_mcp.adapters.postgres.db import lock
from hive_mcp.domain.errors import Conflict, ValidationError
from hive_mcp.domain.promotion import TRANSITIONS, Promotion, PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.adapters.postgres.db import PgDatabase

COLS = (
    "id, org_id, item_id, requester_id, scope_level, scope_owner, visibility, item_revision, "
    "reason, status, created_at, decided_by, decided_at, note"
)


def _row(r: dict[str, Any]) -> Promotion:
    return Promotion(
        id=r["id"],
        org_id=r["org_id"],
        item_id=r["item_id"],
        requester_id=r["requester_id"],
        scope=Scope(ScopeLevel(r["scope_level"]), r["scope_owner"]),
        visibility=Visibility(r["visibility"]),
        item_revision=r["item_revision"],
        created_at=r["created_at"],
        reason=r["reason"],
        status=PromotionStatus(r["status"]),
        decided_by=r["decided_by"],
        decided_at=r["decided_at"],
        note=r["note"],
    )


class PgPromotions:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def create(self, promotion: Promotion) -> Promotion:
        try:
            async with self._db.tx() as conn:
                await lock(conn, "promotion", promotion.item_id)
                await conn.execute(
                    f"INSERT INTO promotions ({COLS}) VALUES ({', '.join(['%s'] * 14)})",  # noqa: S608
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
                        promotion.created_at,
                        promotion.decided_by,
                        promotion.decided_at,
                        promotion.note,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict("duplicate promotion id or item already has a pending request") from exc
        return promotion

    async def get(self, org_id: str, promotion_id: str) -> Promotion | None:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {COLS} FROM promotions WHERE id = %s AND org_id = %s",  # noqa: S608
                (promotion_id, org_id),
            )
            r = await cur.fetchone()
        return _row(r) if r else None

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
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "UPDATE promotions SET status = %s, decided_by = %s, decided_at = %s, note = %s "  # noqa: S608
                f"WHERE id = %s AND org_id = %s AND status = %s RETURNING {COLS}",
                (str(to), by, at, note, promotion_id, org_id, str(expected)),
            )
            r = await cur.fetchone()
        return _row(r) if r else None

    async def delete_for_item(self, org_id: str, item_id: str) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "DELETE FROM promotions WHERE org_id = %s AND item_id = %s", (org_id, item_id)
            )
            return cur.rowcount

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
        pairs = sorted((int(s.level), s.owner) for s in scopes or ())
        async with self._db.tx() as conn:
            cur = await conn.execute(
                f"SELECT {COLS} FROM promotions WHERE org_id = %s "  # noqa: S608
                "AND (%s::text IS NULL OR status = %s) "
                "AND (%s::text IS NULL OR requester_id = %s) "
                "AND (%s OR (scope_level, scope_owner) IN "
                "(SELECT * FROM unnest(%s::smallint[], %s::text[]))) "
                "ORDER BY created_at, id LIMIT %s",
                (
                    org_id,
                    status and str(status),
                    status and str(status),
                    requester_id,
                    requester_id,
                    scopes is None,
                    [lvl for lvl, _ in pairs],
                    [owner for _, owner in pairs],
                    limit,
                ),
            )
            return [_row(r) for r in await cur.fetchall()]
