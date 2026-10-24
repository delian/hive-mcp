"""THE visibility rules. Every store and tool uses this module; nobody re-implements them.

Adapters translate `ScopeFilter` to their native query language and must agree with
`ScopeFilter.tier` (checked by the shared conformance suite).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility, check_visibility


class Tier(StrEnum):
    PRIMARY = "primary"  # inside one of the caller's own scopes
    REFERENCED = "referenced"  # shared org-wide from a scope the caller does not belong to


@dataclass(frozen=True, slots=True)
class Acl:
    """The access-relevant slice of an item; denormalised into search indexes."""

    org_id: str
    scope: Scope
    visibility: Visibility

    def __post_init__(self) -> None:
        check_visibility(self.scope, self.visibility)  # an index can never hold an invalid ACL


@dataclass(frozen=True, slots=True)
class ScopeFilter:
    org_id: str
    member_scopes: frozenset[Scope]

    def tier(self, acl: Acl) -> Tier | None:
        """None means invisible. Tenant boundary first, then membership, then org sharing."""
        if acl.org_id != self.org_id:
            return None
        if acl.scope in self.member_scopes:
            return Tier.PRIMARY
        if acl.visibility is Visibility.ORG:
            return Tier.REFERENCED
        return None


def read_filter(principal: Principal) -> ScopeFilter:
    return ScopeFilter(principal.org_id, principal.member_scopes())


def can_create(principal: Principal, acl: Acl) -> bool:
    return (
        principal.role >= Role.MEMBER
        and acl.org_id == principal.org_id
        and principal.is_member(acl.scope)
    )


def can_write(principal: Principal, acl: Acl, author_id: str) -> bool:
    """Members edit their own items; editors/admins edit anything in scopes they belong to."""
    if not can_create(principal, acl):
        return False
    if acl.scope.level is ScopeLevel.PRIVATE:
        return acl.scope.owner == principal.id
    return author_id == principal.id or principal.role >= Role.EDITOR
