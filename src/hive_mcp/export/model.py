"""The exported rows: column order, types and the optional author pseudonymisation."""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.scope import ScopeLevel

if TYPE_CHECKING:
    from hive_mcp.domain.edge import Edge
    from hive_mcp.domain.item import Item

SCHEMA_VERSION = 1
ITEM_COLUMNS = (
    ("id", "string"),
    ("org", "string"),
    ("kind", "string"),
    ("title", "string"),
    ("body", "string"),
    ("tags", "list"),
    ("status", "string"),
    ("scope_level", "string"),
    ("scope_owner", "string"),
    ("visibility", "string"),
    ("author", "string"),
    ("created_at", "timestamp"),
    ("updated_at", "timestamp"),
    ("revision", "int"),
    ("supersedes", "string"),
    ("source", "string"),
)
EDGE_COLUMNS = (
    ("org", "string"),
    ("src", "string"),
    ("dst", "string"),
    ("relation", "string"),
    ("author", "string"),
    ("created_at", "timestamp"),
)


class Pseudonymiser:
    """Maps person ids to stable aliases within one export. The salt is random per run and is
    never written anywhere, so aliases cannot be reversed or matched across exports."""

    def __init__(self, enabled: bool) -> None:
        self._salt = secrets.token_bytes(16) if enabled else b""
        self.enabled = enabled

    def __call__(self, person_id: str) -> str:
        if not self.enabled:
            return person_id
        digest = hashlib.sha256(self._salt + person_id.encode()).hexdigest()
        return f"p_{digest[:16]}"


def item_row(item: Item, alias: Pseudonymiser) -> dict[str, Any]:
    private = item.scope.level is ScopeLevel.PRIVATE  # the owner of a private scope is a person
    return {
        "id": item.id,
        "org": item.org_id,
        "kind": str(item.kind),
        "title": item.title,
        "body": item.body,
        "tags": sorted(item.tags),
        "status": str(item.status),
        "scope_level": item.scope.level.name.lower(),
        "scope_owner": alias(item.scope.owner) if private else item.scope.owner,
        "visibility": str(item.visibility),
        "author": alias(item.author_id),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "revision": item.revision,
        "supersedes": item.supersedes,
        "source": item.source,
    }


def edge_row(edge: Edge, alias: Pseudonymiser) -> dict[str, Any]:
    return {
        "org": edge.org_id,
        "src": edge.src,
        "dst": edge.dst,
        "relation": edge.relation,
        "author": alias(edge.author_id),
        "created_at": edge.created_at,
    }
