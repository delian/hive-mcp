"""Resources: knowledge as readable documents, for clients that attach resources to a conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.item import ItemQuery
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.instructions import INSTRUCTIONS
from hive_mcp.mcp_api.tools import BODY_LIMIT, current_principal, scope_label
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.item import ItemView

RECENT = 20


def render_item(view: ItemView) -> str:
    """Markdown with the facts an agent must cite: where it lives, who wrote it, whether it is external."""
    it = view.item
    body = it.body[:BODY_LIMIT] + ("\n\n[truncated]" if len(it.body) > BODY_LIMIT else "")
    lines = [
        f"# {it.title}",
        "",
        f"- id: {it.id} (revision {it.revision})",
        f"- kind: {it.kind}, status: {it.status}",
        f"- origin: {provenance(it.scope, view.tier, it.author_id)}",
        f"- scope: {scope_label(it.scope)}, author: {it.author_id}",
        f"- tags: {', '.join(sorted(it.tags)) or '(none)'}",
        f"- updated: {it.updated_at.isoformat()}",
        "",
        body,
    ]
    return "\n".join(lines)


def register_resources(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.resource("kb://guide", title="How to use this knowledge base", mime_type="text/markdown")
    def guide() -> str:
        return INSTRUCTIONS

    @mcp.resource("kb://item/{item_id}", title="Knowledge item", mime_type="text/markdown")
    @guarded
    async def item(item_id: str) -> str:
        return render_item(await get_services().knowledge.get(current_principal(), item_id))

    @mcp.resource("kb://recent", title="Recently added knowledge", mime_type="text/markdown")
    @guarded
    async def recent() -> str:
        page = await get_services().backend.items.list(current_principal(), ItemQuery(limit=RECENT))
        rows = [
            f"- [{v.item.title}](kb://item/{v.item.id}) - {v.item.kind}, "
            f"{provenance(v.item.scope, v.tier, v.item.author_id)}"
            for v in page.items
        ]
        return "# Recent knowledge\n\n" + ("\n".join(rows) or "(nothing yet)")
