"""Promotion workflow: request wider sharing, review, approve or reject.

An approval moves exactly the version the approver was shown: if the item changed after the
request, the request goes stale instead of publishing content nobody reviewed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hive_mcp.domain.errors import Conflict, HiveError, NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.domain.promotion import (
    Promotion,
    PromotionStatus,
    authorize_decision,
    authorize_request,
    clean_text,
)
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.services.knowledge import can_share_directly

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.promotions import PromotionRepository
    from hive_mcp.services.indexing import IndexService

Decision = Literal["approve", "reject", "withdraw"]


@dataclass(frozen=True, slots=True)
class PromotionView:
    promotion: Promotion
    item: Item | None  # content to review; only approvers get it, and only for open requests


class PromotionService:
    def __init__(
        self,
        *,
        items: ItemRepository,
        promotions: PromotionRepository,
        indexing: IndexService,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._items, self._promotions, self._indexing = items, promotions, indexing
        self._audit, self._clock = audit, clock

    async def _log(self, who: Principal, action: str, promo: Promotion) -> None:
        details = {
            "promotion": promo.id,
            "scope": f"{promo.scope.level.name.lower()}:{promo.scope.owner}",
        }
        event = AuditEvent(who.org_id, who.id, action, self._clock.now(), promo.item_id, details)
        await self._audit.append(event)

    async def request(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        reason: str | None = None,
    ) -> Promotion:
        view = await self._items.get(principal, item_id)  # NotFound when invisible
        if can_share_directly(principal, scope, visibility):
            raise ValidationError("you can share this yourself (kb_share); no approval is needed")
        authorize_request(principal, view.item, scope, visibility)
        promo = Promotion(
            id=uuid.uuid4().hex,
            org_id=principal.org_id,
            item_id=item_id,
            requester_id=principal.id,
            scope=scope,
            visibility=visibility,
            item_revision=view.item.revision,
            created_at=self._clock.now(),
            reason=clean_text(reason, "reason"),
        )
        created = await self._promotions.create(promo)
        await self._log(principal, "promotion.request", created)
        return created

    async def list(
        self,
        principal: Principal,
        status: PromotionStatus | None = PromotionStatus.PENDING,
        *,
        mine: bool = False,
    ) -> list[PromotionView]:
        approver = principal.role >= Role.ADMIN and not mine
        found = await self._promotions.list(
            principal.org_id, status=status, requester_id=None if approver else principal.id
        )
        views = []
        for promo in found:
            item = None
            if approver and promo.status is PromotionStatus.PENDING:
                item = await self._items.get_system(principal.org_id, promo.item_id)
            views.append(PromotionView(promo, item))
        return views

    async def decide(
        self, principal: Principal, promotion_id: str, decision: Decision, note: str | None = None
    ) -> Promotion:
        promo = await self._promotions.get(principal.org_id, promotion_id)
        if promo is None:
            raise NotFound(promotion_id)
        text = clean_text(note, "note")
        if decision == "withdraw":
            if promo.requester_id != principal.id:
                raise PermissionDenied("only the requester can withdraw a request")
            return await self._finish(principal, promo, PromotionStatus.WITHDRAWN, text)
        authorize_decision(principal, promo)
        if decision == "reject":
            return await self._finish(principal, promo, PromotionStatus.REJECTED, text)
        if decision != "approve":
            raise ValidationError("decision must be approve, reject or withdraw")
        return await self._approve(principal, promo, text)

    async def _finish(
        self, principal: Principal, promo: Promotion, to: PromotionStatus, note: str
    ) -> Promotion:
        done = await self._promotions.transition(
            promo.org_id, promo.id, PromotionStatus.PENDING, to,
            by=principal.id, note=note, at=self._clock.now(),
        )  # fmt: skip
        if done is None:
            raise Conflict("this request was already decided")
        await self._log(principal, f"promotion.{to}", done)
        return done

    async def _approve(self, principal: Principal, promo: Promotion, note: str) -> Promotion:
        item = await self._items.get_system(promo.org_id, promo.item_id)
        if item is None or item.revision != promo.item_revision:
            await self._finish(principal, promo, PromotionStatus.STALE, "item changed or removed")
            raise Conflict("the item changed after the request; ask for a new promotion")
        claimed = await self._promotions.transition(
            promo.org_id, promo.id, PromotionStatus.PENDING, PromotionStatus.APPROVED,
            by=principal.id, note=note, at=self._clock.now(),
        )  # fmt: skip
        if claimed is None:  # another approver won the race
            raise Conflict("this request was already decided")
        try:
            moved = await self._items.relocate_system(
                promo.org_id, promo.item_id, promo.scope, promo.visibility, promo.item_revision
            )
        except HiveError:
            await self._promotions.transition(
                promo.org_id, promo.id, PromotionStatus.APPROVED, PromotionStatus.STALE,
                by=principal.id, note="move failed after approval", at=self._clock.now(),
            )  # fmt: skip
            raise
        await self._indexing.sync_acl(moved)
        await self._log(principal, "promotion.approved", claimed)
        return claimed
