"""Row <-> domain conversions for Postgres."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hive_mcp.domain.item import Item, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from hive_mcp.domain.access import Acl

ITEM_COLS = (
    "id, org_id, kind, title, body, scope_level, scope_owner, visibility, status, author_id, "
    "created_at, updated_at, revision, supersedes, source, content_hash, tags"
)


def item_params(item: Item) -> tuple[object, ...]:
    return (
        item.id,
        item.org_id,
        str(item.kind),
        item.title,
        item.body,
        int(item.scope.level),
        item.scope.owner,
        str(item.visibility),
        str(item.status),
        item.author_id,
        item.created_at,
        item.updated_at,
        item.revision,
        item.supersedes,
        item.source,
        item.content_hash,
        sorted(item.tags),
    )


def row_to_item(row: dict[str, Any]) -> Item:
    return Item(
        id=row["id"],
        org_id=row["org_id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        body=row["body"],
        scope=Scope(ScopeLevel(row["scope_level"]), row["scope_owner"]),
        author_id=row["author_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        visibility=Visibility(row["visibility"]),
        status=Status(row["status"]),
        tags=frozenset(row["tags"]),
        revision=row["revision"],
        supersedes=row["supersedes"],
        source=row["source"],
    )


def acl_params(acl: Acl) -> tuple[object, ...]:
    return (acl.org_id, int(acl.scope.level), acl.scope.owner, str(acl.visibility))
