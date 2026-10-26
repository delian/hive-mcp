"""Who may sign in and what they belong to: Google proves identity, this file grants access."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import parse_scope, scope_key

log = logging.getLogger("hive.security")

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
    google_sub: str | None = (
        None  # Google account id, pinned at first sign-in (address reuse is refused)
    )

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
            **({"google_sub": self.google_sub} if self.google_sub else {}),
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
            google_sub=str(raw["google_sub"]) if raw.get("google_sub") else None,
        )


class UserDirectory:
    """JSON file of users, re-read when it changes, so admin edits reach live tokens at once."""

    def __init__(self, store: AtomicJsonFile) -> None:
        self._store = store

    def _split(self) -> tuple[dict[str, DirectoryUser], list[Any]]:
        """Valid users, and the unreadable rows (kept as they are when the file is rewritten)."""
        users: dict[str, DirectoryUser] = {}
        bad: list[Any] = []
        for raw in self._store.read():
            try:
                user = DirectoryUser.from_json(raw)
            except (KeyError, ValueError, TypeError, AttributeError, ValidationError):
                log.error("directory.bad_row (skipped; that user cannot sign in): %r", raw)
                bad.append(raw)  # one typo in users.json must not lock out everybody else
                continue
            users[user.email] = user
        return users, bad

    def _all(self) -> dict[str, DirectoryUser]:
        return self._split()[0]

    def _save(self, users: dict[str, DirectoryUser], bad: list[Any]) -> None:
        rows = [u.to_json() for u in sorted(users.values(), key=lambda u: u.email)]
        self._store.write([*rows, *bad])

    def lookup(self, email: str) -> DirectoryUser | None:
        """None for unknown *and* disabled users (a disabled user's tokens stop working at once)."""
        try:
            user = self._all().get(normalize_email(email))
        except ValidationError:
            return None
        return None if user is None or user.disabled else user

    def upsert(self, user: DirectoryUser) -> None:
        users, bad = self._split()
        users[user.email] = user
        self._save(users, bad)

    def remove(self, email: str) -> bool:
        users, bad = self._split()
        if users.pop(normalize_email(email), None) is None:
            return False
        self._save(users, bad)
        return True

    def permits(self, subject: str) -> bool:
        """Static tokens and admin sessions whose subject is an email belong to a person in this
        directory and work only while that person is present and not disabled. Other subjects
        (service accounts, pipelines) are not people and are not checked here."""
        return "@" not in subject or self.lookup(subject) is not None

    def list(self) -> list[DirectoryUser]:
        return sorted(self._all().values(), key=lambda u: u.email)
