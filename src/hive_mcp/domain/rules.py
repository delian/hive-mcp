"""Write-side authorisation shared by every ItemRepository (so adapters cannot drift apart)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import Conflict, PermissionDenied, ValidationError

if TYPE_CHECKING:
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal


MAX_KEY_LEN = 200


def check_idempotency_key(key: str | None) -> None:
    if key is not None and (not key or len(key) > MAX_KEY_LEN or "\x00" in key):
        raise ValidationError(f"idempotency key must be 1-{MAX_KEY_LEN} characters without NUL")


def authorize_create(principal: Principal, item: Item, idempotency_key: str | None = None) -> None:
    check_idempotency_key(idempotency_key)
    if item.author_id != principal.id or not access.can_create(principal, item.acl):
        raise PermissionDenied("cannot create in this scope")


def _check_revision(current: Item, expected_revision: int) -> None:
    if current.revision != expected_revision:
        raise Conflict(f"revision {current.revision} != expected {expected_revision}")


def authorize_update(principal: Principal, current: Item, expected_revision: int) -> None:
    """The caller already proved the item is visible. Permission is checked before the revision."""
    if not access.can_write(principal, current.acl, current.author_id):
        raise PermissionDenied(current.id)
    _check_revision(current, expected_revision)


def authorize_relocate(
    principal: Principal, current: Item, moved: Item, expected_revision: int
) -> None:
    """`moved` is the already-validated target (see `relocate_item`)."""
    if not access.can_write(principal, current.acl, current.author_id):
        raise PermissionDenied(current.id)
    if not access.can_create(principal, moved.acl):
        raise PermissionDenied("not a member of the target scope")
    _check_revision(current, expected_revision)
