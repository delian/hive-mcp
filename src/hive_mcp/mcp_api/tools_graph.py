"""Tools for linking items and exploring the graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal, scope_label
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


class NodeOut(BaseModel):
    id: str
    title: str
    kind: str
    scope: str
    origin: str
    external: bool = Field(description="True when it belongs to a group you are not in.")
    depth: int


class EdgeOut(BaseModel):
    from_id: str
    to_id: str
    relation: str


class GraphOut(BaseModel):
    nodes: list[NodeOut] = Field(description="The first node is the item you asked about.")
    edges: list[EdgeOut]
    truncated: bool = Field(description="True when more neighbours exist than were returned.")


class LinkOut(BaseModel):
    created: bool
    message: str


def register_graph_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_link(from_id: str, to_id: str, relation: str = "related") -> LinkOut:
        """Link two items, e.g. a finding `derived_from` another group's research.

        Needs write access to `from_id` and read access to `to_id`. `relation` is a short word:
        related, part_of, next, derived_from, supports, contradicts, supersedes, mentions, or your own.
        Others see the link only if they can see both items.
        """
        created = await get_services().graph.link(current_principal(), from_id, to_id, relation)
        return LinkOut(created=created, message="Linked." if created else "Already linked.")

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_unlink(from_id: str, to_id: str, relation: str = "related") -> LinkOut:
        """Remove a link you created, or one from an item you may edit."""
        await get_services().graph.unlink(current_principal(), from_id, to_id, relation)
        return LinkOut(created=False, message="Unlinked.")

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_graph(
        id: str, depth: int = 1, relations: list[str] | None = None, max_nodes: int = 30
    ) -> GraphOut:
        """Explore what an item is connected to (1-3 hops), only through items you can see.

        Use after kb_consult to follow sources, parts of a document, or related decisions.
        """
        result = await get_services().graph.neighbours(
            current_principal(),
            id,
            depth=depth,
            relations=frozenset(relations or ()),
            max_nodes=max_nodes,
        )
        nodes = [
            NodeOut(
                id=n.view.item.id,
                title=n.view.item.title,
                kind=str(n.view.item.kind),
                scope=scope_label(n.view.item.scope),
                origin=provenance(n.view.item.scope, n.view.tier, n.view.item.author_id),
                external=n.view.tier.value == "referenced",
                depth=n.depth,
            )
            for n in result.nodes
        ]
        edges = [EdgeOut(from_id=e.src, to_id=e.dst, relation=e.relation) for e in result.edges]
        return GraphOut(nodes=nodes, edges=edges, truncated=result.truncated)
