"""Promotion requests: a member asks for knowledge to be shared wider; an approver decides.

Rules live here so every service and adapter applies the same ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass field
from enum import StrEnum
from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.item import Item, Status

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility

MAX_TEXT = 2000


class PromotionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    STALE = "stale"  # the item changed (or vanished) after the request, so approval was refused


# Allowed moves. APPROVED -> STALE only when the move itself failed after the decision was claimed.
TRANSITIONS: dict[PromotionStatus, frozenset[PromotionStatus]] = {
    PromotionStatus.PENDING: frozenset(
        {
            PromotionStatus.APPROVED,
            PromotionStatus.REJECTED,
            PromotionStatus.WITHDRAWN,
            PromotionStatus.STALE,
        }
    ),
    PromotionStatus.APPROVED: frozenset({PromotionStatus.STALE}),
}


@dataclass(frozen=True, slots=True)
class Promotion:
    id: str
    org_id: str
    item_id: str
    requester_id: str
    scope: Scope  # where the item would go
    visibility: Visibility
    item_revision: int  # the exact version the approver is shown and approves
    created_at: datetime
    reason: str = ""
    status: PromotionStatus = PromotionStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str = ""


def clean_text(value: str | None, what: str) -> str:
    text = (value or "").strip()
    if len(text) > MAX_TEXT or "\x00" in text:
        raise ValidationError(f"{what} must be at most {MAX_TEXT} characters without NUL")
    return text


def authorize_request(
    principal: Principal, item: Item, scope: Scope, visibility: Visibility
) -> None:
    """The requester must be able to edit the item and belong to the place it is going to."""
    target = access.Acl(item.org_id, scope, visibility)  # validates scope/visibility together
    if item.org_id != principal.org_id or not access.can_write(principal, item.acl, item.author_id):
        raise PermissionDenied(item.id)
    if item.status is Status.ARCHIVED:
        raise ValidationError("archived items cannot be promoted")
    if target == item.acl:
        raise ValidationError("the item is already there")
    if not access.can_create(principal, target):
        raise PermissionDenied("not a member of the target scope")


def authorize_decision(principal: Principal, promotion: Promotion) -> None:
    """Admins, or the named approvers of the target scope; nobody approves their own request."""
    if principal.org_id != promotion.org_id or not principal.can_approve(promotion.scope):
        raise PermissionDenied("only an approver of the target scope (or an admin) can decide")
    if principal.id == promotion.requester_id:
        raise PermissionDenied("you cannot decide your own request; ask another approver")
