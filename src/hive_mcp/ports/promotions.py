"""Store for promotion requests. Decisions are atomic compare-and-set transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.promotion import Promotion, PromotionStatus
    from hive_mcp.domain.scope import Scope


class PromotionRepository(Protocol):
    async def create(self, promotion: Promotion) -> Promotion:
        """Conflict if the item already has a PENDING request or the id exists."""

    async def get(self, org_id: str, promotion_id: str) -> Promotion | None:
        """Never crosses orgs."""

    async def transition(  # noqa: PLR0913 - compare-and-set needs all of these
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
        """Atomically move `expected` -> `to` (must be a legal move). None when the request is
        missing or no longer in `expected`: exactly one of several concurrent deciders wins."""

    async def delete_for_item(self, org_id: str, item_id: str) -> int:
        """Remove every request about the item (it is being purged); returns how many."""

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
        """Oldest first (a review queue) unless `newest_first`; only this org's requests. `scopes`
        keeps requests whose target is one of them (None = any; an empty set = none)."""
