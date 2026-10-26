"""kb_digest: what is new in the places the user can read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, current_principal
from hive_mcp.services.digest import DigestItem, render_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


class DigestItemOut(BaseModel):
    id: str
    title: str
    kind: str
    author: str
    origin: str = Field(description="'EXTERNAL: ...' means another group's knowledge.")
    tags: list[str]
    edited_by: str | None = None


class DigestOut(BaseModel):
    since: str
    new_items: int = Field(description="How many new items there are; `items` may show fewer.")
    items: list[DigestItemOut]
    top_tags: dict[str, int]
    edited: list[DigestItemOut] = Field(
        description="Older items that someone else changed in the window."
    )
    pending_approvals: int = Field(
        description="Promotion requests waiting for you (approvers only)."
    )
    text: str = Field(description="The same digest as readable text.")


def _out(i: DigestItem) -> DigestItemOut:
    return DigestItemOut(
        id=i.id, title=i.title, kind=i.kind, author=i.author, origin=i.origin, tags=list(i.tags),
        edited_by=i.edited_by,
    )  # fmt: skip


def register_digest_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_digest(
        days: int = 7, limit: int = 30, include_external: bool = False
    ) -> DigestOut:
        """What is new for the user: items others added in the last `days` days to places they can read.

        Use it at the start of a week or a project to catch up. Their own items and personal session
        logs are left out. `include_external` adds knowledge shared from other groups.
        """
        digest = await get_services().digests.build(
            current_principal(), days=days, limit=limit, include_external=include_external
        )
        return DigestOut(
            since=digest.since.isoformat(),
            new_items=digest.total_new,
            items=[_out(i) for i in digest.items],
            edited=[_out(i) for i in digest.edited],
            top_tags=dict(digest.top_tags),
            pending_approvals=digest.pending_approvals,
            text=render_text(digest),
        )  # fmt: skip
