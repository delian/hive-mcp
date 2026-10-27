"""System of record for knowledge items.

Every method takes the calling `Principal`. Adapters apply `domain.access` themselves (defence in
depth) and raise `NotFound` for items the caller cannot see, so existence never leaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.item import Item, ItemPatch, ItemQuery, ItemView, Page, Status
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope, Visibility


@dataclass(frozen=True, slots=True)
class CreateResult:
    item: Item
    created: bool  # False when an idempotency key replayed an earlier create


class ItemRepository(Protocol):
    async def create(
        self, principal: Principal, item: Item, idempotency_key: str | None = None
    ) -> CreateResult:
        """Idempotent per (org, author, key): same payload replays, a different
        one raises IdempotencyConflict."""

    async def get(self, principal: Principal, item_id: str) -> ItemView: ...

    async def get_system(self, org_id: str, item_id: str) -> Item | None:
        """Trusted internal read for indexing/maintenance jobs: no user ACL, never across orgs.
        Must not be reachable from any tool."""

    async def list_ids_system(self, org_id: str, after: str | None, limit: int) -> list[str]:
        """Trusted maintenance listing: ids of every item in the org (any status, any ACL),
        ascending, strictly after `after`. For reindexing; must not be reachable from any tool."""

    async def get_many(self, principal: Principal, item_ids: Sequence[str]) -> list[ItemView]:
        """Hydrate search hits. Invisible or missing ids are dropped; order follows `item_ids`."""

    async def update(
        self, principal: Principal, item_id: str, patch: ItemPatch, expected_revision: int
    ) -> Item:
        """Conflict if `expected_revision` is stale; PermissionDenied if read-only."""

    async def relocate(
        self,
        principal: Principal,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        """Promote/demote; caller must write the item and belong to the target."""

    async def tag_counts(
        self, principal: Principal, *, prefix: str = "", limit: int = 100
    ) -> list[tuple[str, int]]:
        """Tags on active/draft items the caller can see, with item counts: most used first, then
        by name. `prefix` keeps tags that start with it (exact match of the leading characters)."""

    async def relocate_system(
        self,
        org_id: str,
        item_id: str,
        scope: Scope,
        visibility: Visibility,
        expected_revision: int,
    ) -> Item:
        """Move on behalf of an approved promotion: no user ACL, never across orgs, and still
        guarded by `expected_revision`. The approval service has already done the authorising."""

    async def set_status_system(
        self, org_id: str, item_id: str, status: Status, expected_revision: int
    ) -> Item:
        """Change the status on behalf of a system rule (retention): no user ACL, never across
        orgs, still guarded by `expected_revision`, and recorded as a new revision by "system"."""

    async def list(self, principal: Principal, query: ItemQuery) -> Page:
        """Newest first, keyset-paginated: no repeats or skips, even with equal timestamps."""
