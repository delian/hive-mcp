"""Tells people about promotion requests and decisions, through durable per-channel jobs.

A failed delivery is retried by the job runner without repeating the channels that worked. Item
content is never sent, only the title, who asked and where to (approvers read the item in the tool).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.promotion import Promotion, PromotionStatus
from hive_mcp.domain.scope import Visibility, scope_key
from hive_mcp.ports.notify import Notification

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.jobs import Job, JobQueue
    from hive_mcp.ports.notify import Notifier
    from hive_mcp.ports.promotions import PromotionRepository
    from hive_mcp.security.directory import UserDirectory

NOTIFY_JOB = "notify"
REQUESTED = "promotion.requested"
DECIDED = "promotion.decided"
log = logging.getLogger(__name__)


def build_notification(
    event: str, promo: Promotion, title: str, recipients: tuple[str, ...]
) -> Notification:
    target = scope_key(promo.scope) + (" (org-wide)" if promo.visibility is Visibility.ORG else "")
    if event == REQUESTED:
        reason = f"\nReason: {promo.reason}" if promo.reason else ""
        return Notification(
            event,
            f"Promotion request: {title}",
            f'{promo.requester_id} asks to share "{title}" with {target}.{reason}\n'
            "To review, ask your agent to run kb_list_promotions, then kb_decide_promotion.",
            recipients,
        )
    verdict = {
        PromotionStatus.APPROVED: "approved",
        PromotionStatus.REJECTED: "rejected",
        PromotionStatus.STALE: "could not be applied because the item changed",
    }.get(promo.status, str(promo.status))
    note = f"\nNote: {promo.note}" if promo.note else ""
    return Notification(
        event,
        f"Your promotion request was {verdict}: {title}",
        f'Your request to share "{title}" with {target} was {verdict} by {promo.decided_by}.{note}',
        recipients,
    )


class NotificationService:
    def __init__(
        self,
        *,
        channels: Mapping[str, Notifier],
        promotions: PromotionRepository,
        items: ItemRepository,
        directory: UserDirectory,
        jobs: JobQueue,
    ) -> None:
        self._channels, self._promotions, self._items = channels, promotions, items
        self._directory, self._jobs = directory, jobs

    async def enqueue(self, event: str, promo: Promotion) -> None:
        """Best effort: a notification problem never fails the request or the decision."""
        for channel in self._channels:
            try:
                await self._jobs.enqueue(
                    promo.org_id,
                    NOTIFY_JOB,
                    {"channel": channel, "event": event, "promotion_id": promo.id},
                    dedupe_key=f"notify:{event}:{promo.id}:{channel}:{promo.status}",
                )
            except HiveError:
                log.warning("could not queue %s notification for %s", event, promo.id)

    def _recipients(self, event: str, promo: Promotion) -> tuple[str, ...]:
        if event == DECIDED:
            return (promo.requester_id,) if "@" in promo.requester_id else ()
        found = []
        for user in self._directory.list():
            who = user.principal()
            if (
                not user.disabled
                and user.org_id == promo.org_id
                and who.can_approve(promo.scope)
                and user.email != promo.requester_id
            ):
                found.append(user.email)
        return tuple(found)

    async def handle(self, job: Job) -> None:
        """Job handler; raising (NotifyError) makes the runner retry this one channel."""
        notifier = self._channels.get(job.payload["channel"])
        promo = await self._promotions.get(job.org_id, job.payload["promotion_id"])
        if notifier is None or promo is None:
            return  # channel removed from the configuration, or request gone
        event = job.payload["event"]
        item = await self._items.get_system(promo.org_id, promo.item_id)
        title = item.title if item else "(removed item)"
        await notifier.send(build_notification(event, promo, title, self._recipients(event, promo)))
