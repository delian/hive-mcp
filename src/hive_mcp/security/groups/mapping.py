"""Which Workspace group grants what, and the pure computation of the resulting directory."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.domain.scope import parse_scope, scope_key
from hive_mcp.security.directory import DirectoryUser, normalize_email

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class Grant:
    teams: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()
    approves: frozenset[str] = frozenset()  # scope keys such as "team:eng"
    role: Role | None = None


@dataclass(frozen=True, slots=True)
class GroupMap:
    org_id: str
    groups: dict[str, Grant]
    auto_create: bool = False
    default_role: Role = Role.MEMBER
    disable_missing: bool = (
        False  # disable users in none of the groups (admins are never auto-disabled)
    )
    allowed_domains: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def parse(raw: Mapping[str, Any], allowed_domains: list[str] | None = None) -> GroupMap:
        try:
            groups = {}
            for email, spec in dict(raw["groups"]).items():
                role = Role[str(spec["role"]).upper()] if spec.get("role") else None
                approves = frozenset(scope_key(parse_scope(k)) for k in spec.get("approves", []))
                groups[normalize_email(email)] = Grant(
                    frozenset(spec.get("teams", [])),
                    frozenset(spec.get("projects", [])),
                    approves,
                    role,
                )
            return GroupMap(
                org_id=str(raw["org_id"]),
                groups=groups,
                auto_create=bool(raw.get("auto_create", False)),
                default_role=Role[str(raw.get("default_role", "member")).upper()],
                disable_missing=bool(raw.get("disable_missing", False)),
                allowed_domains=frozenset(d.casefold() for d in (allowed_domains or [])),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValidationError(f"invalid group map: {exc!r}") from exc

    def universe(self) -> Grant:
        """Every value the mapping can grant: these, and only these, are owned by the sync."""
        return Grant(
            frozenset().union(*(g.teams for g in self.groups.values())),
            frozenset().union(*(g.projects for g in self.groups.values())),
            frozenset().union(*(g.approves for g in self.groups.values())),
        )


@dataclass(frozen=True, slots=True)
class Change:
    email: str
    action: str  # created | updated | disabled | enabled
    before: DirectoryUser | None
    after: DirectoryUser


def _desired(mapping: GroupMap, groups_of: list[Grant]) -> Grant:
    roles = [g.role for g in groups_of if g.role is not None]
    return Grant(
        frozenset().union(*(g.teams for g in groups_of)),
        frozenset().union(*(g.projects for g in groups_of)),
        frozenset().union(*(g.approves for g in groups_of)),
        max(roles) if roles else None,
    )


def plan(
    mapping: GroupMap, members: Mapping[str, set[str]], current: list[DirectoryUser]
) -> list[Change]:
    """What to change so the directory matches the groups. Pure: nothing is written here."""
    universe = mapping.universe()
    has_role_groups = any(g.role is not None for g in mapping.groups.values())
    by_user: dict[str, list[Grant]] = {}
    for group, emails in members.items():
        for email in emails:
            by_user.setdefault(email, []).append(mapping.groups[group])
    existing = {u.email: u for u in current}
    changes = []
    for email in sorted(set(existing) | set(by_user)):
        user, grants = existing.get(email), by_user.get(email, [])
        if user is None:
            domain = email.rpartition("@")[2]
            if not (
                mapping.auto_create
                and (not mapping.allowed_domains or domain in mapping.allowed_domains)
            ):
                continue
            user = DirectoryUser(email, mapping.org_id, mapping.default_role)
            before = None
        else:
            before = user
        want = _desired(mapping, grants)
        role = user.role
        if has_role_groups and grants:
            role = want.role or mapping.default_role  # managed users: groups decide, none = default
        after = replace(
            user,
            role=role,
            teams=(user.teams - universe.teams) | want.teams,
            projects=(user.projects - universe.projects) | want.projects,
            approves=(user.approves - universe.approves) | want.approves,
        )
        if (
            mapping.disable_missing
            and not grants
            and not after.disabled
            and after.role < Role.ADMIN
        ):
            after = replace(after, disabled=True)
        elif grants and after.disabled and before is not None:
            after = replace(after, disabled=False)  # back in a group: access returns
        if before is None:
            changes.append(Change(email, "created", None, after))
        elif after != before:
            action = (
                "disabled"
                if after.disabled and not before.disabled
                else ("enabled" if before.disabled and not after.disabled else "updated")
            )
            changes.append(Change(email, action, before, after))
    return changes
