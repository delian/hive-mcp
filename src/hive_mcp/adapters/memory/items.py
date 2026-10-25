"""In-memory ItemRepository. Single-threaded event loop: no await inside a critical section."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.errors import Conflict, IdempotencyConflict, NotFound
from hive_mcp.domain.item import Cursor, Item, ItemView, Page, apply_patch, relocate_item
from hive_mcp.domain.rules import (
    authorize_create,
    authorize_relocate,
    authorize_update,
    check_revision,
)
from hive_mcp.ports.items import CreateResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import ItemPatch, ItemQuery
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility


class MemoryItems:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._items: dict[str, Item] = {}
        self._idem: dict[
            tuple[str, str, str], tuple[str, str]
        ] = {}  # key -> (fingerprint, item_id)

    def _visible(self, principal: Principal, item_id: str) -> ItemView:
        item = self._items.get(item_id)
        tier = access.read_filter(principal).tier(item.acl) if item else None
        if item is None or tier is None:
            raise NotFound(item_id)
        return ItemView(item, tier)

    async def create(
        self, principal: Principal, item: Item, idempotency_key: str | None = None
    ) -> CreateResult:
        authorize_create(principal, item, idempotency_key)
        slot = (principal.org_id, principal.id, idempotency_key or "")
        if idempotency_key and slot in self._idem:
            fingerprint, existing = self._idem[slot]
            if fingerprint != item.fingerprint():
                raise IdempotencyConflict(idempotency_key)
            return CreateResult(self._items[existing], created=False)
        if item.id in self._items:  # validate everything before recording the key
            raise Conflict(f"id exists: {item.id}")
        if idempotency_key:
            self._idem[slot] = (item.fingerprint(), item.id)
        self._items[item.id] = item
        return CreateResult(item, created=True)

    async def get(self, principal: Principal, item_id: str) -> ItemView:
        return self._visible(principal, item_id)

    async def get_system(self, org_id: str, item_id: str) -> Item | None:
        item = self._items.get(item_id)
        return item if item and item.org_id == org_id else None

    async def list_ids_system(self, org_id: str, after: str | None, limit: int) -> list[str]:
        ids = sorted(i.id for i in self._items.values() if i.org_id == org_id)
        return [i for i in ids if after is None or i > after][:limit]

    async def get_many(self, principal: Principal, item_ids: Sequence[str]) -> list[ItemView]:
        views = []
        for item_id in item_ids:
            try:
                views.append(self._visible(principal, item_id))
            except NotFound:
                continue
        return views

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        view = self._visible(principal, item_id)
        authorize_update(principal, view.item, expected_revision)
        updated = apply_patch(view.item, patch, self._clock.now())
        self._items[item_id] = updated
        return updated

    async def relocate(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        view = self._visible(principal, item_id)
        moved = relocate_item(view.item, scope, visibility, self._clock.now())
        authorize_relocate(principal, view.item, moved, expected_revision)
        self._items[item_id] = moved
        return moved

    async def relocate_system(
        self,
        org_id: str,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        current = self._items.get(item_id)
        if current is None or current.org_id != org_id:
            raise NotFound(item_id)
        check_revision(current, expected_revision)
        moved = relocate_item(current, scope, visibility, self._clock.now())
        self._items[item_id] = moved
        return moved

    async def list(self, principal: Principal, query: ItemQuery) -> Page:
        flt = access.read_filter(principal)
        rows = []
        for item in self._items.values():
            tier = flt.tier(item.acl)
            if tier is not None and query.matches(item, tier):
                rows.append(ItemView(item, tier))
        rows.sort(key=lambda v: (v.item.created_at, v.item.id), reverse=True)
        page, more = rows[: query.limit], len(rows) > query.limit
        last = page[-1].item if page else None
        cursor = Cursor(last.created_at, last.id).encode() if more and last else None
        return Page(tuple(page), cursor)
