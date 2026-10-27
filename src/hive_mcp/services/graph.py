"""The knowledge graph: typed links between items, traversed with the caller's own visibility.

An edge is returned only when the caller can see BOTH ends, so links never reveal that an item
they cannot see exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.edge import Edge, normalize_relation
from hive_mcp.domain.errors import NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import ItemView, Status
from hive_mcp.domain.text import check_text
from hive_mcp.ports.audit import AuditEvent

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.edges import EdgeRepository
    from hive_mcp.ports.items import ItemRepository

MAX_DEPTH = 3
MAX_NODES = 100


@dataclass(frozen=True, slots=True)
class GraphNode:
    view: ItemView
    depth: int


@dataclass(frozen=True, slots=True)
class GraphResult:
    nodes: list[GraphNode]  # nodes[0] is the start item
    edges: list[Edge]
    truncated: bool  # more neighbours exist than were returned


class GraphService:
    def __init__(
        self, *, items: ItemRepository, edges: EdgeRepository, audit: AuditLog, clock: Clock
    ) -> None:
        self._items, self._edges, self._audit, self._clock = items, edges, audit, clock

    async def _log(self, who: Principal, action: str, edge: Edge) -> None:
        details = {"dst": edge.dst, "relation": edge.relation}
        await self._audit.append(
            AuditEvent(who.org_id, who.id, action, self._clock.now(), edge.src, details)
        )

    async def link(
        self, principal: Principal, src: str, dst: str, relation: str = "related"
    ) -> bool:
        """Link an item you may edit to any item you can read (your own or another group's).
        True if the link is new; repeating it is harmless."""
        edge_relation = normalize_relation(relation)
        check_text(src, dst, what="id")
        source = await self._items.get(principal, src)
        await self._items.get(principal, dst)  # NotFound when invisible
        if not access.can_write(principal, source.item.acl, source.item.author_id):
            raise PermissionDenied(src)
        edge = Edge(principal.org_id, src, dst, edge_relation, principal.id, self._clock.now())
        created = await self._edges.add(edge)
        if created:
            await self._log(principal, "edge.add", edge)
        return created

    async def unlink(self, principal: Principal, src: str, dst: str, relation: str) -> None:
        check_text(src, dst, what="id")
        edge = await self._edges.get(principal.org_id, src, dst, normalize_relation(relation))
        if edge is None:
            raise NotFound("no such link")
        if edge.author_id != principal.id:
            try:
                source = await self._items.get(principal, src)
            except NotFound:
                raise NotFound("no such link") from None
            if not access.can_write(principal, source.item.acl, source.item.author_id):
                raise PermissionDenied(
                    "only the link's author or an editor of its source can remove it"
                )
        await self._edges.remove(principal.org_id, src, dst, edge.relation)
        await self._log(principal, "edge.remove", edge)

    async def neighbours(
        self,
        principal: Principal,
        item_id: str,
        *,
        depth: int = 1,
        relations: frozenset[str] = frozenset(),
        max_nodes: int = 50,
    ) -> GraphResult:
        if not 1 <= depth <= MAX_DEPTH or not 1 <= max_nodes <= MAX_NODES:
            raise ValidationError(f"depth must be 1-{MAX_DEPTH} and max_nodes 1-{MAX_NODES}")
        check_text(item_id, what="id")
        wanted = frozenset(normalize_relation(r) for r in relations)
        start = await self._items.get(principal, item_id)
        seen: dict[str, GraphNode] = {item_id: GraphNode(start, 0)}
        edges: dict[tuple[str, str, str], Edge] = {}
        frontier, truncated = [item_id], False
        for level in range(1, depth + 1):
            found = await self._edges.incident(principal.org_id, frontier, relations=wanted)
            truncated = truncated or len(found) >= 200  # noqa: PLR2004 - repository page size
            others = {e.src if e.dst in frontier else e.dst for e in found} - set(seen)
            views = {
                v.item.id: v
                for v in await self._items.get_many(principal, sorted(others))
                if v.item.status is not Status.ARCHIVED
            }
            next_frontier: list[str] = []
            for item_id_, view in views.items():
                if len(seen) >= max_nodes:
                    truncated = True
                    break
                seen[item_id_] = GraphNode(view, level)
                next_frontier.append(item_id_)
            for edge in found:
                if edge.src in seen and edge.dst in seen:
                    edges[edge.key] = edge
            frontier = next_frontier
            if not frontier:
                break
        return GraphResult(list(seen.values()), list(edges.values()), truncated)
