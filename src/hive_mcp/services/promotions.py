"""Promotion workflow: request wider sharing, review, approve or reject.

An approval moves exactly the version the approver was shown: if the item changed after the
request, the request goes stale instead of publishing content nobody reviewed.
"""

from __future__ import annotations

import logging
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
from hive_mcp.services.notifications import DECIDED, REQUESTED

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.jobs import Job, JobQueue
    from hive_mcp.ports.promotions import PromotionRepository
    from hive_mcp.services.indexing import IndexService
    from hive_mcp.services.notifications import NotificationService

APPLY_JOB = "apply_promotion"
APPLY_DELAY_SECONDS = 60.0
log = logging.getLogger(__name__)
Decision = Literal["approve", "reject", "withdraw"]


@dataclass(frozen=True, slots=True)
class PromotionView:
    promotion: Promotion
    item: Item | None  # content to review; only approvers get it, and only for open requests


class PromotionService:
    def __init__(  # noqa: PLR0913 - collaborators
        self,
        *,
        items: ItemRepository,
        promotions: PromotionRepository,
        indexing: IndexService,
        jobs: JobQueue,
        audit: AuditLog,
        clock: Clock,
        notifications: NotificationService | None = None,
    ) -> None:
        self._items, self._promotions, self._indexing = items, promotions, indexing
        self._jobs, self._audit, self._clock = jobs, audit, clock
        self._notify = notifications

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
        await self._announce(REQUESTED, created)
        return created

    async def list(
        self,
        principal: Principal,
        status: PromotionStatus | None = PromotionStatus.PENDING,
        *,
        mine: bool = False,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[PromotionView]:
        approver = principal.is_approver and not mine
        admin = principal.role >= Role.ADMIN
        found = await self._promotions.list(
            principal.org_id,
            status=status,
            requester_id=None if approver else principal.id,
            scopes=principal.approves if approver and not admin else None,
            limit=limit,
            newest_first=newest_first,
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
        if to in {PromotionStatus.REJECTED, PromotionStatus.STALE}:
            await self._announce(DECIDED, done)
        return done

    async def _announce(self, event: str, promo: Promotion) -> None:
        if self._notify:
            await self._notify.enqueue(event, promo)

    async def _apply(self, promo: Promotion) -> None:
        """Move the item to the approved place (safe to repeat): raises Conflict or NotFound when
        the item changed or vanished since the request, anything else is transient."""
        item = await self._items.get_system(promo.org_id, promo.item_id)
        if item is None:
            raise NotFound(promo.item_id)
        if item.scope == promo.scope and item.visibility == promo.visibility:
            moved = item  # already there (an earlier attempt got this far)
        else:
            moved = await self._items.relocate_system(
                promo.org_id, promo.item_id, promo.scope, promo.visibility, promo.item_revision
            )
        await self._indexing.sync_acl(moved)

    async def _stale(self, who: str, promo: Promotion, why: str) -> None:
        done = await self._promotions.transition(
            promo.org_id, promo.id, PromotionStatus.APPROVED, PromotionStatus.STALE,
            by=who, note=why, at=self._clock.now(),
        )  # fmt: skip
        if done:
            await self._announce(DECIDED, done)

    async def _approve(self, principal: Principal, promo: Promotion, note: str) -> Promotion:
        item = await self._items.get_system(promo.org_id, promo.item_id)
        if item is None or item.revision != promo.item_revision:
            await self._finish(principal, promo, PromotionStatus.STALE, "item changed or removed")
            raise Conflict("the item changed after the request; ask for a new promotion")
        # The repair job is queued BEFORE the decision is recorded: if this process dies half way,
        # the job finishes the move (and does nothing if the decision never got recorded).
        await self._jobs.enqueue(
            promo.org_id, APPLY_JOB, {"promotion_id": promo.id},
            dedupe_key=f"apply:{promo.id}", delay_seconds=APPLY_DELAY_SECONDS,
        )  # fmt: skip
        claimed = await self._promotions.transition(
            promo.org_id, promo.id, PromotionStatus.PENDING, PromotionStatus.APPROVED,
            by=principal.id, note=note, at=self._clock.now(),
        )  # fmt: skip
        if claimed is None:  # another approver won the race
            raise Conflict("this request was already decided")
        try:
            await self._apply(promo)
        except (Conflict, NotFound):
            await self._stale(principal.id, promo, "item changed after approval")
            raise Conflict("the item changed while approving; ask for a new promotion") from None
        except HiveError:
            log.warning("promotion %s approved; the move will be completed by a job", promo.id)
        await self._log(principal, "promotion.approved", claimed)
        await self._announce(DECIDED, claimed)
        return claimed

    async def handle_apply(self, job: Job) -> None:
        """Job handler: completes approvals whose move did not finish. Raises to be retried."""
        promo = await self._promotions.get(job.org_id, job.payload["promotion_id"])
        if promo is None or promo.status is not PromotionStatus.APPROVED:
            return  # never approved (crash before the decision) or already settled
        try:
            await self._apply(promo)
        except (Conflict, NotFound):
            await self._stale("system", promo, "item changed after approval")
