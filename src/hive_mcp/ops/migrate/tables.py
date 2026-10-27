"""What is copied, and how each value looks in the neutral form both stores are read into.

Copied: items (with tags), their earlier versions, idempotency keys, the audit trail, the
key-value records (integration settings, connector state, confirmations...), promotions, edges and
stored files. Not copied, because the target rebuilds or does not need it: the search and vector
indexes (rebuilt by indexing every item), queued jobs, rate-limit buckets and expired records.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    times: tuple[str, ...] = ()  # columns that are timestamps
    jsons: tuple[str, ...] = ()  # columns that hold a JSON object
    lists: tuple[str, ...] = ()  # columns that hold a list of tags


TABLES = (
    Table(
        "items",
        (
            "id",
            "org_id",
            "kind",
            "title",
            "body",
            "scope_level",
            "scope_owner",
            "visibility",
            "status",
            "author_id",
            "created_at",
            "updated_at",
            "revision",
            "supersedes",
            "source",
            "content_hash",
            "tags",
        ),
        ("id",),
        times=("created_at", "updated_at"),
        lists=("tags",),
    ),
    Table(
        "item_revisions",
        (
            "item_id",
            "org_id",
            "revision",
            "title",
            "body",
            "kind",
            "status",
            "tags",
            "scope_level",
            "scope_owner",
            "visibility",
            "item_author",
            "written_at",
            "replaced_at",
            "replaced_by",
        ),
        ("item_id", "revision"),
        times=("written_at", "replaced_at"),
        lists=("tags",),
    ),
    Table(
        "idempotency",
        ("org_id", "author_id", "key", "fingerprint", "item_id"),
        ("org_id", "author_id", "key"),
    ),
    Table(
        "audit",
        ("seq", "org_id", "actor_id", "action", "target_id", "at", "details"),
        ("seq",),
        times=("at",),
        jsons=("details",),
    ),
    Table(
        "kv",
        ("namespace", "key", "value", "expires_at"),
        ("namespace", "key"),
        times=("expires_at",),
        jsons=("value",),
    ),
    Table(
        "promotions",
        (
            "id",
            "org_id",
            "item_id",
            "requester_id",
            "scope_level",
            "scope_owner",
            "visibility",
            "item_revision",
            "reason",
            "status",
            "created_at",
            "decided_by",
            "decided_at",
            "note",
        ),
        ("id",),
        times=("created_at", "decided_at"),
    ),
    Table(
        "edges",
        ("org_id", "src", "dst", "relation", "author_id", "created_at"),
        ("org_id", "src", "dst", "relation"),
        times=("created_at",),
    ),
)
BY_NAME = {t.name: t for t in TABLES}
Row = dict[str, Any]


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def canonical(table: Table | None, row: Row) -> str:
    """One row as text that is identical whichever store it came from."""
    out: Row = {}
    for name, value in row.items():
        if isinstance(value, datetime):
            out[name] = utc_text(value)
        elif isinstance(value, list):
            out[name] = sorted(value)
        else:
            out[name] = value
    return json.dumps(out, sort_keys=True, default=str, ensure_ascii=False)


class Fingerprint:
    """Count and an order-independent digest (the stores sort text differently)."""

    def __init__(self) -> None:
        self.count, self._sum = 0, 0

    def add(self, table: Table | None, row: Row) -> None:
        self.count += 1
        digest = hashlib.sha256(canonical(table, row).encode()).digest()
        self._sum = (self._sum + int.from_bytes(digest, "big")) % (1 << 256)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Fingerprint) and (self.count, self._sum) == (
            other.count,
            other._sum,
        )

    def __hash__(self) -> int:
        return hash((self.count, self._sum))

    def __repr__(self) -> str:
        return f"{self.count} rows, digest {self._sum:064x}"[:60]
