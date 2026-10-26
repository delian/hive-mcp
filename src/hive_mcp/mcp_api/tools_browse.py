"""Tools for browsing without a search query: list items, list tags."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from hive_mcp.domain.item import Cursor, ItemQuery, Status
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, _kinds, current_principal, scope_label
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


class ListItemOut(BaseModel):
    id: str
    title: str
    kind: str
    tags: list[str]
    status: str
    scope: str
    origin: str
    external: bool
    updated_at: str
    revision: int


class ListOut(BaseModel):
    items: list[ListItemOut]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null at the end."
    )


class TagOut(BaseModel):
    tag: str
    items: int


STATUS_SETS = {
    "current": frozenset({Status.ACTIVE, Status.DRAFT}),
    "all": frozenset(Status),
    **{s.value: frozenset({s}) for s in Status},
}


def register_browse_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_list(  # noqa: PLR0913, PLR0917 - one parameter per tool argument
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
        status: Literal["current", "all", "draft", "active", "superseded", "archived"] = "current",
        mine_only: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ListOut:
        """Browse items newest first without a search query, optionally by kind and tags (all must match).

        `mine_only` hides other groups' items. Use `cursor` from the previous page to continue.
        For finding things by meaning, use kb_consult instead.
        """
        query = ItemQuery(
            kinds=_kinds(kinds),
            tags_all=normalize_tags(tags or []),
            statuses=STATUS_SETS[status],
            include_referenced=not mine_only,
            limit=limit,
            after=Cursor.decode(cursor) if cursor else None,
        )
        page = await get_services().knowledge.browse(current_principal(), query)
        items = [
            ListItemOut(
                id=v.item.id,
                title=v.item.title,
                kind=str(v.item.kind),
                tags=sorted(v.item.tags),
                status=str(v.item.status),
                scope=scope_label(v.item.scope),
                origin=provenance(v.item.scope, v.tier, v.item.author_id),
                external=v.tier.value == "referenced",
                updated_at=v.item.updated_at.isoformat(),
                revision=v.item.revision,
            )
            for v in page.items
        ]
        return ListOut(items=items, next_cursor=page.next_cursor)

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_tags(prefix: str = "", limit: int = 100) -> list[TagOut]:
        """List the tags in use (with item counts) among items you can see, most used first.

        Look here before tagging, so you reuse existing tags instead of inventing near-duplicates.
        `prefix` narrows to a family, e.g. `domain/`.
        """
        rows = await get_services().knowledge.tags(current_principal(), prefix, limit)
        return [TagOut(tag=t, items=n) for t, n in rows]
