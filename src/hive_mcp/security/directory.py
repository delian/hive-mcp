"""Who may sign in and what they belong to: Google proves identity, this file grants access."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import parse_scope, scope_key

if TYPE_CHECKING:
    from hive_mcp.security.jsonfile import AtomicJsonFile

_EMAIL = re.compile(r"^[^@\s\x00]{1,64}@[^@\s\x00]{1,255}$")
MAX_EMAIL_LEN = 254


def normalize_email(raw: str) -> str:
    email = raw.strip().casefold()
    if len(email) > MAX_EMAIL_LEN or not _EMAIL.match(email):
        raise ValidationError(f"invalid email address: {raw!r}")
    return email


def _strings(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError("teams and projects must be lists of strings")
    return frozenset(value)


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    email: str
    org_id: str
    role: Role = Role.MEMBER
    teams: frozenset[str] = field(default_factory=frozenset)
    projects: frozenset[str] = field(default_factory=frozenset)
    disabled: bool = False
    approves: frozenset[str] = field(default_factory=frozenset)  # scope keys, e.g. "team:t1"

    def principal(self) -> Principal:
        approves = frozenset(parse_scope(k) for k in self.approves)
        return Principal(self.email, self.org_id, self.role, self.teams, self.projects, approves)

    def to_json(self) -> dict[str, object]:
        return {
            "email": self.email,
            "org_id": self.org_id,
            "role": self.role.name.lower(),
            "teams": sorted(self.teams),
            "projects": sorted(self.projects),
            "disabled": self.disabled,
            "approves": sorted(self.approves),
        }

    @staticmethod
    def from_json(raw: dict[str, object]) -> DirectoryUser:
        return DirectoryUser(
            email=normalize_email(str(raw["email"])),
            org_id=str(raw["org_id"]),
            role=Role[str(raw.get("role", "viewer")).upper()],
            teams=_strings(raw.get("teams")),
            projects=_strings(raw.get("projects")),
            disabled=bool(raw.get("disabled", False)),
            approves=frozenset(scope_key(parse_scope(k)) for k in _strings(raw.get("approves"))),
        )


class UserDirectory:
    """JSON file of users, re-read when it changes, so admin edits reach live tokens at once."""

    def __init__(self, store: AtomicJsonFile) -> None:
        self._store = store

    def _all(self) -> dict[str, DirectoryUser]:
        users = (DirectoryUser.from_json(r) for r in self._store.read())
        return {u.email: u for u in users}

    def lookup(self, email: str) -> DirectoryUser | None:
        """None for unknown *and* disabled users (a disabled user's tokens stop working at once)."""
        try:
            user = self._all().get(normalize_email(email))
        except ValidationError:
            return None
        return None if user is None or user.disabled else user

    def upsert(self, user: DirectoryUser) -> None:
        users = self._all()
        users[user.email] = user
        self._store.write([u.to_json() for u in sorted(users.values(), key=lambda u: u.email)])

    def remove(self, email: str) -> bool:
        users = self._all()
        if users.pop(normalize_email(email), None) is None:
            return False
        self._store.write([u.to_json() for u in users.values()])
        return True

    def list(self) -> list[DirectoryUser]:
        return sorted(self._all().values(), key=lambda u: u.email)
