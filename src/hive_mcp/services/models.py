"""Request/response types of the services (plain dataclasses; MCP tools map to and from them)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass annotation
from enum import StrEnum

from hive_mcp.domain.access import Tier
from hive_mcp.domain.item import Item, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility


class ForeignPolicy(StrEnum):
    AUTO = "auto"  # add other groups' knowledge only when your own has too few matches
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class RememberRequest:
    title: str
    body: str
    kind: Kind = Kind.NOTE
    tags: tuple[str, ...] = ()
    scope: Scope | None = None  # None = private to the caller
    visibility: Visibility = Visibility.SCOPE
    idempotency_key: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class RememberResult:
    item: Item
    created: bool  # False when an idempotency key replayed an earlier call
    indexed: bool  # False when indexing was deferred to a background job
    redactions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConsultRequest:
    query: str
    limit: int = 8
    kinds: frozenset[Kind] = frozenset()
    tags_all: frozenset[str] = frozenset()
    statuses: frozenset[Status] = frozenset({Status.ACTIVE, Status.DRAFT})
    include_foreign: ForeignPolicy = ForeignPolicy.AUTO
    min_primary: int = 3  # AUTO adds foreign results when fewer of your own matched


@dataclass(frozen=True, slots=True)
class Match:
    item_id: str
    title: str
    snippet: str
    kind: Kind
    tags: frozenset[str]
    status: Status
    score: float
    tier: Tier
    scope: Scope
    author_id: str
    updated_at: datetime
    revision: int
    provenance: str  # human-readable origin, always shown for referenced results


@dataclass(frozen=True, slots=True)
class ConsultResult:
    primary: tuple[Match, ...]
    referenced: tuple[Match, ...]
    notes: tuple[str, ...] = ()


def provenance(scope: Scope, tier: Tier, author_id: str) -> str:
    where = {
        ScopeLevel.PRIVATE: "your private notes",
        ScopeLevel.PROJECT: f"project {scope.owner}",
        ScopeLevel.TEAM: f"team {scope.owner}",
        ScopeLevel.ORG: "organisation-wide knowledge",
    }[scope.level]
    if tier is Tier.PRIMARY:
        return where
    return f"EXTERNAL: shared from {where} by {author_id} (not your group)"
