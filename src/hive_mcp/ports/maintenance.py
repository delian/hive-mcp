"""Trusted maintenance operations on items (purge, retention scans). Never reachable from a tool
without the service layer checking who is asking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.item import Item, Kind


@dataclass(frozen=True, slots=True)
class ItemFilter:
    org_id: str | None = None  # None = every organisation (retention sweeps)
    author_id: str | None = None
    kinds: frozenset[Kind] = field(default_factory=frozenset)  # empty = any
    created_before: datetime | None = None
    private_only: bool = False


@dataclass(frozen=True, slots=True)
class AuthorUsage:
    author_id: str
    items: int  # not archived
    chars: int  # characters of title + body over those items
    last_at: datetime


class ItemMaintenance(Protocol):
    async def purge_system(self, org_id: str, item_id: str) -> Item | None:
        """Permanently delete the item row, its tags and its idempotency records in one
        transaction. Returns what was deleted (None if absent or in another org). Indexes, edges,
        promotions and blobs are the caller's job."""

    async def find_system(
        self, flt: ItemFilter, *, after_id: str | None = None, limit: int = 100
    ) -> list[Item]:
        """Items matching the filter (any status), ascending by id, strictly after `after_id`."""

    async def usage(self, org_id: str, author_id: str | None = None) -> list[AuthorUsage]:
        """Items and text volume per author (archived items excluded), biggest first. With
        `author_id`, only that author (an empty list if they have nothing)."""

    async def count_system(
        self, org_id: str | None = None, *, include_archived: bool = False
    ) -> int:
        """Number of items (all orgs when `org_id` is None), by default without archived ones."""

    async def count_by_source(self, org_id: str, source: str) -> int:
        """How many items in the org reference this `source` string (blob reference counting)."""
