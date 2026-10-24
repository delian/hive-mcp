"""The authenticated caller. Built only from verified token claims, never from tool arguments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from hive_mcp.domain.scope import Scope, ScopeLevel


class Role(IntEnum):
    VIEWER = 0  # read only
    MEMBER = 1  # read + write own knowledge
    EDITOR = 2  # may edit any item in scopes they belong to
    ADMIN = 3  # editor + approvals


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    org_id: str
    role: Role = Role.MEMBER
    teams: frozenset[str] = field(default_factory=frozenset)
    projects: frozenset[str] = field(default_factory=frozenset)

    def member_scopes(self) -> frozenset[Scope]:
        """Every scope this caller belongs to (their private space, projects, teams, the org)."""
        return frozenset(
            {Scope(ScopeLevel.PRIVATE, self.id), Scope(ScopeLevel.ORG, self.org_id)}
            | {Scope(ScopeLevel.PROJECT, p) for p in self.projects}
            | {Scope(ScopeLevel.TEAM, t) for t in self.teams}
        )

    def is_member(self, scope: Scope) -> bool:
        return scope in self.member_scopes()
