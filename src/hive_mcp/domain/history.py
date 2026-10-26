"""Item versions: what an item said at each revision, who replaced it, and who may read it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass field
from typing import TYPE_CHECKING

from hive_mcp.domain.access import Acl
from hive_mcp.domain.item import Item, Kind, Status  # noqa: TC001 - runtime-evaluated annotations
from hive_mcp.domain.scope import Scope, Visibility  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.access import ScopeFilter

KEEP_VERSIONS = 50  # stored previous versions per item; older ones are dropped


@dataclass(frozen=True, slots=True)
class ItemVersion:
    item_id: str
    org_id: str
    revision: int
    title: str
    body: str
    kind: Kind
    status: Status
    tags: frozenset[str]
    scope: Scope  # where the item lived at THIS revision
    visibility: Visibility
    item_author: str
    written_at: datetime
    replaced_at: datetime | None = None  # None for the current version
    replaced_by: str | None = None

    @property
    def current(self) -> bool:
        return self.replaced_at is None

    @property
    def acl(self) -> Acl:
        return Acl(self.org_id, self.scope, self.visibility)


def current_version(item: Item) -> ItemVersion:
    return ItemVersion(
        item.id, item.org_id, item.revision, item.title, item.body, item.kind, item.status,
        item.tags, item.scope, item.visibility, item.author_id, item.updated_at,
    )  # fmt: skip


def readable(flt: ScopeFilter, versions: Sequence[ItemVersion]) -> list[ItemVersion]:
    """Keep only versions the caller could have read when they were current. A version written
    while the item was private stays private even after the item is shared."""
    return [v for v in versions if flt.tier(v.acl) is not None]
