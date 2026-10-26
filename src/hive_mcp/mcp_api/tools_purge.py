"""Permanent deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from hive_mcp.domain.errors import ValidationError
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import current_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services

DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


class PurgeOut(BaseModel):
    id: str
    title: str
    message: str


def register_purge_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=DESTRUCTIVE)
    @guarded
    async def kb_purge(id: str, confirm: str) -> PurgeOut:
        """PERMANENTLY delete an item and everything attached to it (search entries, links, stored log). Cannot be undone.

        Only the item's author or an admin may do this. To merely hide an item use kb_update with
        status=archived. Pass the same id as `confirm` to prove it is intended; ask the user first.
        """
        if confirm != id:
            raise ValidationError("confirm must repeat the item id exactly")
        item = await get_services().purge.purge(current_principal(), id)
        return PurgeOut(id=item.id, title=item.title, message="Permanently deleted.")
