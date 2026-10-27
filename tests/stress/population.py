"""A mixed population of people and notes plus an access oracle that does not use the product's
own access code: it works from plain tables of memberships."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain.item import Item, Kind, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from tests.stress.support import rng

if TYPE_CHECKING:
    from random import Random

    from hive_mcp.services.consult import ConsultService  # noqa: F401

# (id, org, role, projects, teams)
PEOPLE = [
    ("m1", "acme", Role.MEMBER, {"p1"}, {"t1"}),
    ("m2", "acme", Role.MEMBER, {"p1", "p2"}, set()),
    ("m3", "acme", Role.MEMBER, {"p3"}, {"t2"}),
    ("m4", "acme", Role.MEMBER, set(), set()),
    ("v1", "acme", Role.VIEWER, {"p1"}, set()),
    ("e1", "acme", Role.EDITOR, {"p2"}, {"t1"}),
    ("adm", "acme", Role.ADMIN, {"p4"}, {"t3"}),
    ("g1", "globex", Role.MEMBER, {"p1"}, {"t1"}),  # same group ids as acme, another tenant
    ("g2", "globex", Role.ADMIN, {"p2"}, {"t2"}),
]
PROJECTS = ("p1", "p2", "p3", "p4")
TEAMS = ("t1", "t2", "t3")
STATUSES = (Status.ACTIVE,) * 8 + (Status.DRAFT, Status.ARCHIVED)


def principal(row: tuple) -> Principal:
    pid, org, role, projects, teams = row
    return Principal(pid, org, role, teams=frozenset(teams), projects=frozenset(projects))


@dataclass
class Note:
    id: str
    org: str
    level: str  # "private" | "project" | "team" | "org"
    owner: str
    visibility: str  # "scope" | "org"
    status: Status
    token: str
    created: object = None


def memberships(row: tuple) -> set[tuple[str, str]]:
    pid, org, _role, projects, teams = row
    return (
        {("private", pid), ("org", org)}
        | {("project", p) for p in projects}
        | {("team", t) for t in teams}
    )


def oracle(row: tuple, note: Note) -> str | None:
    """'primary', 'referenced' or None, from first principles."""
    if note.org != row[1]:
        return None
    if (note.level, note.owner) in memberships(row):
        return "primary"
    return "referenced" if note.visibility == "org" else None


@dataclass
class Population:
    notes: dict[str, Note] = field(default_factory=dict)
    by_token: dict[str, Note] = field(default_factory=dict)

    def listing(self, row: tuple, statuses: frozenset[Status] | None = None) -> set[str]:
        wanted = statuses or frozenset({Status.ACTIVE, Status.DRAFT})
        return {i for i, n in self.notes.items() if oracle(row, n) and n.status in wanted}


def _scope_of(note: Note) -> Scope:
    return Scope(ScopeLevel[note.level.upper()], note.owner)


async def build(stack, count: int, seed: int = 0) -> Population:
    """Create `count` notes (written straight to the repository and indexed, which is much faster
    than the service) with a deliberate mix of scopes, visibility and status. Most notes share one
    created_at; a handful of other timestamps exist so ordering has real work to do."""
    gen: Random = rng(seed)
    base = stack.backend.clock.now()
    pop = Population()
    members = {
        org: [r for r in PEOPLE if r[1] == org and r[2] >= Role.MEMBER]
        for org in ("acme", "globex")
    }
    sem = asyncio.Semaphore(8)
    plans = [
        (
            "acme" if gen.random() < 0.85 else "globex",
            gen.choices(["private", "project", "team", "org"], [3, 4, 2, 2])[0],
            gen.choice(PROJECTS),
            gen.choice(TEAMS),
            gen.randrange(1000),
            gen.random() < 0.3,
            gen.choice(STATUSES),
            0 if gen.random() < 0.8 else gen.choice((-30, -5, 7, 90)),
        )
        for _ in range(count)
    ]

    def pick(org: str, level: str, project: str, team: str, salt: int) -> tuple[str, str, tuple]:
        """(level, owner, author) for a scope; falls back to the org when nobody belongs."""
        rows = members[org]
        if level == "private":
            author = rows[salt % len(rows)]
            return level, author[0], author
        if level in ("project", "team"):
            owner = project if level == "project" else team
            index = 3 if level == "project" else 4
            candidates = [r for r in rows if owner in r[index]]
            if candidates:
                return level, owner, candidates[salt % len(candidates)]
        return "org", org, rows[salt % len(rows)]

    async def make(i: int) -> None:
        org, level, project, team, salt, org_visible, status, minutes = plans[i]
        level, owner, author = pick(org, level, project, team, salt)
        visibility = "org" if level != "private" and org_visible else "scope"
        note = Note(
            f"n{i:06d}", org, level, owner, visibility, status, f"nt{i}x",
            created=base + timedelta(minutes=minutes),
        )  # fmt: skip
        item = Item(
            id=note.id,
            org_id=org,
            kind=Kind.NOTE,
            title=f"Note {note.token}",
            body=f"{note.token} pelican shared words group{i % 40} {level} {owner} " * 3,
            scope=_scope_of(note),
            author_id=author[0],
            created_at=note.created,
            updated_at=note.created,
            visibility=Visibility(visibility),
            status=status,
        )
        async with sem:
            await stack.backend.items.create(principal(author), item)
            await stack.indexing.index_item(item)
        pop.notes[note.id] = note
        pop.by_token[note.token] = note

    await asyncio.gather(*(make(i) for i in range(count)))
    return pop
