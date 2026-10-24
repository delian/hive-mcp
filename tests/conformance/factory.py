"""Test data: a small org with overlapping memberships, and an item builder."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from hive_mcp.domain.item import Item, Kind
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock

ALICE = Principal("alice", "org1", Role.MEMBER, teams=frozenset({"t1"}), projects=frozenset({"p1"}))
BOB = Principal("bob", "org1", Role.MEMBER, projects=frozenset({"p1"}))
CAROL = Principal("carol", "org1", Role.MEMBER, projects=frozenset({"p2"}))
EDDIE = Principal("eddie", "org1", Role.EDITOR, projects=frozenset({"p1"}))
VIVI = Principal("vivi", "org1", Role.VIEWER, projects=frozenset({"p1"}))
ROOT = Principal("root", "org1", Role.ADMIN, projects=frozenset({"p2"}))  # an approver
ZOE = Principal("zoe", "org2", Role.ADMIN, projects=frozenset({"p1"}))  # other tenant, same ids

P1 = Scope(ScopeLevel.PROJECT, "p1")
P2 = Scope(ScopeLevel.PROJECT, "p2")
ALICE_PRIVATE = Scope(ScopeLevel.PRIVATE, "alice")
ORG1 = Scope(ScopeLevel.ORG, "org1")

_ids = itertools.count(1)


def make_item(  # noqa: PLR0913 - test builder
    clock: Clock,
    author: Principal = ALICE,
    scope: Scope = P1,
    *,
    title: str = "Radar range equation",
    body: str = "Range follows from pulse delay.",
    visibility: Visibility = Visibility.SCOPE,
    kind: Kind = Kind.NOTE,
    tags: frozenset[str] = frozenset(),
    item_id: str | None = None,
) -> Item:
    now = clock.now()
    return Item(
        id=item_id or f"item{next(_ids):04d}",
        org_id=author.org_id,
        kind=kind,
        title=title,
        body=body,
        scope=scope,
        author_id=author.id,
        created_at=now,
        updated_at=now,
        visibility=visibility,
        tags=tags,
    )
