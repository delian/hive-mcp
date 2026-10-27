"""Who did how much lately: tool calls, writes and storage per person and per team (admins only).

Built from the audit trail (so it covers every replica and survives restarts) and the storage
totals. It shows counts, never content. Bounded: at most MAX_EVENTS events are read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.ops.usage import ACTION as USAGE_ACTION

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditEvent, AuditLog
    from hive_mcp.ports.maintenance import ItemMaintenance

MAX_DAYS = 365
MAX_EVENTS = 50_000
PAGE = 500
WRITE_ACTIONS = frozenset({"item.create", "item.update", "item.relocate", "item.purge"})


@dataclass
class PersonActivity:
    user: str
    calls: int = 0
    writes: int = 0
    items: int = 0
    characters: int = 0
    last_seen: datetime | None = None
    tools: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class Activity:
    days: int
    people: list[PersonActivity]
    teams: dict[str, tuple[int, int]]  # team -> (calls, writes)
    tools: list[tuple[str, int]]  # busiest tools overall
    truncated: bool  # more events existed than MAX_EVENTS: the oldest were left out


class ActivityReport:
    def __init__(
        self,
        audit: AuditLog,
        maintenance: ItemMaintenance,
        clock: Clock,
        teams_of: Callable[[str], frozenset[str]] = lambda _user: frozenset(),
    ) -> None:
        self._audit, self._maintenance, self._clock, self._teams_of = (
            audit, maintenance, clock, teams_of,
        )  # fmt: skip

    async def build(self, admin: Principal, days: int = 30) -> Activity:
        if admin.role < Role.ADMIN:
            raise PermissionDenied("this needs an admin")
        if not 1 <= days <= MAX_DAYS:
            raise ValidationError(f"days must be 1-{MAX_DAYS}")
        since = self._clock.now() - timedelta(days=days)
        people, truncated = await self._from_audit(admin.org_id, since)
        for usage in await self._maintenance.usage(admin.org_id):
            person = people.setdefault(usage.author_id, PersonActivity(usage.author_id))
            person.items, person.characters = usage.items, usage.chars
        ranked = sorted(people.values(), key=lambda p: (-p.calls, -p.writes, p.user))
        teams: dict[str, tuple[int, int]] = {}
        tools: Counter[str] = Counter()
        for p in ranked:
            tools.update(p.tools)
            for team in self._teams_of(p.user):
                calls, writes = teams.get(team, (0, 0))
                teams[team] = (calls + p.calls, writes + p.writes)
        return Activity(days, ranked, teams, tools.most_common(10), truncated)

    async def _from_audit(
        self, org_id: str, since: datetime
    ) -> tuple[dict[str, PersonActivity], bool]:
        """Newest events first, until the window ends or MAX_EVENTS have been read."""
        people: dict[str, PersonActivity] = {}
        seen, before = 0, None
        while page := await self._audit.list(
            org_id, limit=PAGE, before_seq=before, newest_first=True
        ):
            before = page[-1].seq
            for record in page:
                if record.event.at < since:
                    return people, False  # everything further back is older still
                seen += 1
                self._add(people, record.event)
            if seen >= MAX_EVENTS:
                return people, True
        return people, False

    @staticmethod
    def _add(people: dict[str, PersonActivity], event: AuditEvent) -> None:
        if event.action != USAGE_ACTION and event.action not in WRITE_ACTIONS:
            return
        person = people.setdefault(event.actor_id, PersonActivity(event.actor_id))
        if event.action == USAGE_ACTION:
            person.calls += int(event.details.get("calls", "0") or 0)
            for pair in filter(None, event.details.get("tools", "").split(",")):
                name, _, n = pair.partition("=")
                person.tools[name] += int(n or 0)
        else:
            person.writes += 1
        person.last_seen = max(filter(None, [person.last_seen, event.at]))
