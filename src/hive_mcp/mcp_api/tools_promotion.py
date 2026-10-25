"""Tools for asking for wider sharing and for approvers to decide."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.promotion import Promotion, PromotionStatus
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal, scope_label

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.services.promotions import PromotionView

PREVIEW_LIMIT = 4000


class PromotionOut(BaseModel):
    id: str
    item_id: str
    status: str
    requested_by: str
    target: str = Field(description="Where the item would go, e.g. team:t1 or org:acme.")
    org_wide: bool = Field(
        description="Whether other groups in the org would see it (as external)."
    )
    item_revision: int
    reason: str
    decided_by: str | None
    note: str
    review_title: str | None = Field(default=None, description="Approvers only, open requests.")
    review_body: str | None = Field(default=None, description="Approvers only; may be truncated.")
    review_scope: str | None = Field(default=None, description="Where the item lives now.")
    review_author: str | None = None


def _out(promo: Promotion, view: PromotionView | None = None) -> PromotionOut:
    item = view.item if view else None
    return PromotionOut(
        id=promo.id,
        item_id=promo.item_id,
        status=str(promo.status),
        requested_by=promo.requester_id,
        target=scope_label(promo.scope),
        org_wide=promo.visibility is Visibility.ORG,
        item_revision=promo.item_revision,
        reason=promo.reason,
        decided_by=promo.decided_by,
        note=promo.note,
        review_title=item.title if item else None,
        review_body=item.body[:PREVIEW_LIMIT] if item else None,
        review_scope=scope_label(item.scope) if item else None,
        review_author=item.author_id if item else None,
    )


def _target(level: str, owner: str | None, org_wide: bool, org_id: str) -> tuple[Scope, Visibility]:
    if level == "org":
        return Scope(ScopeLevel.ORG, org_id), Visibility.SCOPE
    if not owner:
        raise ValidationError(f"name the {level} (owner) to share with")
    return Scope(ScopeLevel[level.upper()], owner), Visibility.ORG if org_wide else Visibility.SCOPE


def register_promotion_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_request_promotion(
        id: str,
        to: Literal["project", "team", "org"],
        owner: str | None = None,
        org_wide: bool = False,
        reason: str | None = None,
    ) -> PromotionOut:
        """Ask an approver to share an item you wrote with a team or the whole organisation.

        `to`: project or team (name it in `owner`), or org. `org_wide`: let other groups in the
        organisation see it, labelled external. The approver reviews the item as it is now; if you edit
        it afterwards the request goes stale and you must ask again.
        """
        who = current_principal()
        scope, visibility = _target(to, owner, org_wide, who.org_id)
        promo = await get_services().promotions.request(who, id, scope, visibility, reason)
        return _out(promo)

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_list_promotions(
        status: Literal["pending", "approved", "rejected", "withdrawn", "stale", "all"] = "pending",
        mine: bool = False,
    ) -> list[PromotionOut]:
        """List promotion requests. Approvers see the whole organisation's queue with the item
        content to review; everyone else sees their own requests. `mine` limits approvers to their own."""
        wanted = None if status == "all" else PromotionStatus(status)
        views = await get_services().promotions.list(current_principal(), wanted, mine=mine)
        return [_out(v.promotion, v) for v in views]

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_decide_promotion(
        id: str, decision: Literal["approve", "reject", "withdraw"], note: str | None = None
    ) -> PromotionOut:
        """Approvers approve or reject a request (not their own); requesters can withdraw theirs.

        Approving moves the item immediately. Say why when rejecting."""
        promo = await get_services().promotions.decide(current_principal(), id, decision, note)
        return _out(promo)
