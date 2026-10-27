"""'What did the agent use?': the notes an agent was shown and opened during one work session.

An agent passes the same `session` label to kb_consult / kb_get / kb_report_session. The server
remembers, per person and session, which notes were shown and which were opened (ids only, kept
for a day, at most 100 each). `kb_session_sources` lists them with where each came from and
whether it was confirmed lately, so a human can review what a result was built on; and
`kb_report_session` links the stored session note `derived_from` the notes that were opened, so
the trail stays in the knowledge graph after the label expires. Access is checked again whenever
the list is read: a note that became private or was deleted simply is not listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.item import ItemView
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.services.graph import GraphService

NS = "session:used"
TTL_SECONDS = 24 * 3600
MAX_IDS = 100
MAX_LINKS = 20
LABEL = re.compile(r"[A-Za-z0-9._:-]{4,80}")


@dataclass(frozen=True, slots=True)
class Source:
    view: ItemView
    opened: bool  # False: it was only shown in search results
    origin: str


def _key(principal: Principal, session: str) -> str:
    if not LABEL.fullmatch(session):
        raise ValidationError("session labels are 4-80 letters, digits and . _ : -")
    return f"{principal.org_id}:{principal.id}:{session}"


class SessionSources:
    def __init__(self, items: ItemRepository, kv: KeyValueStore, graph: GraphService) -> None:
        self._items, self._kv, self._graph = items, kv, graph

    async def record(
        self,
        principal: Principal,
        session: str,
        *,
        shown: Sequence[str] = (),
        opened: Sequence[str] = (),
    ) -> None:
        key = _key(principal, session)
        state = await self._kv.get(NS, key) or {"shown": [], "opened": []}
        for field, ids in (("shown", shown), ("opened", opened)):
            merged = list(dict.fromkeys([*state[field], *ids]))
            state[field] = merged[-MAX_IDS:]
        await self._kv.put(NS, key, state, ttl_seconds=TTL_SECONDS)

    async def sources(self, principal: Principal, session: str) -> list[Source]:
        state = await self._kv.get(NS, _key(principal, session))
        if not state:
            return []
        opened = set(state["opened"])
        ids = list(dict.fromkeys([*state["opened"], *state["shown"]]))
        views = {v.item.id: v for v in await self._items.get_many(principal, ids)}
        return [
            Source(
                views[i],
                i in opened,
                provenance(views[i].item.scope, views[i].tier, views[i].item.author_id),
            )
            for i in ids
            if i in views
        ]

    async def link(self, principal: Principal, session: str, session_item_id: str) -> int:
        """Record the opened notes as the session note's sources in the graph; returns how many."""
        done = 0
        for source in await self.sources(principal, session):
            if not source.opened or done >= MAX_LINKS:
                continue
            try:
                await self._graph.link(
                    principal, session_item_id, source.view.item.id, "derived_from"
                )
                done += 1
            except HiveError:
                continue  # a note we may read but not link (another group's): skip it
        return done
