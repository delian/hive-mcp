"""In-memory PromotionRepository (reference behaviour for the conformance suite)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import Conflict, ValidationError
from hive_mcp.domain.promotion import TRANSITIONS, Promotion, PromotionStatus

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.scope import Scope


class MemoryPromotions:
    def __init__(self) -> None:
        self._rows: dict[str, Promotion] = {}

    async def create(self, promotion: Promotion) -> Promotion:
        if promotion.id in self._rows:
            raise Conflict(f"id exists: {promotion.id}")
        if any(
            p.item_id == promotion.item_id and p.status is PromotionStatus.PENDING
            for p in self._rows.values()
        ):
            raise Conflict("this item already has a pending promotion request")
        self._rows[promotion.id] = promotion
        return promotion

    async def get(self, org_id: str, promotion_id: str) -> Promotion | None:
        row = self._rows.get(promotion_id)
        return row if row and row.org_id == org_id else None

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
        row = await self.get(org_id, promotion_id)
        if row is None or row.status is not expected:
            return None
        updated = replace(row, status=to, decided_by=by, decided_at=at, note=note)
        self._rows[promotion_id] = updated
        return updated

    async def delete_for_item(self, org_id: str, item_id: str) -> int:
        doomed = [k for k, p in self._rows.items() if p.org_id == org_id and p.item_id == item_id]
        for key in doomed:
            del self._rows[key]
        return len(doomed)

    async def list(  # noqa: PLR0913 - keyword-only filters
        self,
        org_id: str,
        *,
        status: PromotionStatus | None = None,
        requester_id: str | None = None,
        scopes: frozenset[Scope] | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[Promotion]:
        rows = [
            p
            for p in self._rows.values()
            if p.org_id == org_id
            and (status is None or p.status is status)
            and (requester_id is None or p.requester_id == requester_id)
            and (scopes is None or p.scope in scopes)
        ]
        rows.sort(key=lambda p: (p.created_at, p.id), reverse=newest_first)
        return rows[:limit]
