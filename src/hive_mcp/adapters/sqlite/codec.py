"""Row <-> domain conversions. Timestamps are stored as fixed-width UTC ISO text so text order == time order."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hive_mcp.domain.item import Item, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.domain.access import Acl

ITEM_COLS = (
    "id, org_id, kind, title, body, scope_level, scope_owner, visibility, status, author_id, "
    "created_at, updated_at, revision, supersedes, source, content_hash"
)


def ts(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


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
        ts(item.created_at),
        ts(item.updated_at),
        item.revision,
        item.supersedes,
        item.source,
        item.content_hash,
    )


def row_to_item(row: sqlite3.Row, tags: frozenset[str]) -> Item:
    return Item(
        id=row["id"],
        org_id=row["org_id"],
        kind=Kind(row["kind"]),
        title=row["title"],
        body=row["body"],
        scope=Scope(ScopeLevel(row["scope_level"]), row["scope_owner"]),
        author_id=row["author_id"],
        created_at=parse_ts(row["created_at"]),
        updated_at=parse_ts(row["updated_at"]),
        visibility=Visibility(row["visibility"]),
        status=Status(row["status"]),
        tags=tags,
        revision=row["revision"],
        supersedes=row["supersedes"],
        source=row["source"],
    )


def acl_params(acl: Acl) -> tuple[object, ...]:
    return (acl.org_id, int(acl.scope.level), acl.scope.owner, str(acl.visibility))
