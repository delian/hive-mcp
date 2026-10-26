"""Earlier versions of an item: list, read, compare, restore."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.schemas import ChangeOut  # noqa: TC001 - the tool output model, resolved at runtime
from hive_mcp.mcp_api.tools import (
    BODY_LIMIT,
    READ,
    WRITE,
    _change_out,
    current_principal,
    scope_label,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.history import ItemVersion


class VersionSummary(BaseModel):
    revision: int
    title: str
    current: bool
    written_at: str
    replaced_at: str | None
    replaced_by: str | None = Field(
        description="Who made the next change (None for the current version)."
    )
    scope: str = Field(description="Where the item lived at that revision.")


class VersionOut(VersionSummary):
    body: str
    truncated: bool
    kind: str
    status: str
    tags: list[str]


def _summary(v: ItemVersion) -> VersionSummary:
    return VersionSummary(
        revision=v.revision,
        title=v.title,
        current=v.current,
        written_at=v.written_at.isoformat(),
        replaced_at=v.replaced_at.isoformat() if v.replaced_at else None,
        replaced_by=v.replaced_by,
        scope=scope_label(v.scope),
    )


def register_history_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_history(id: str, limit: int = 20) -> list[VersionSummary]:
        """List an item's versions, newest first (the last 50 are kept). Versions written while the item
        was still private to someone else are not shown to you."""
        versions = await get_services().knowledge.history(current_principal(), id, limit)
        return [_summary(v) for v in versions]

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_version(id: str, revision: int) -> VersionOut:
        """Read one earlier version of an item in full."""
        v = await get_services().knowledge.version(current_principal(), id, revision)
        return VersionOut(
            **_summary(v).model_dump(),
            body=v.body[:BODY_LIMIT],
            truncated=len(v.body) > BODY_LIMIT,
            kind=str(v.kind),
            status=str(v.status),
            tags=sorted(v.tags),
        )

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_diff(id: str, from_revision: int, to_revision: int | None = None) -> str:
        """What changed between two versions of an item (unified diff of title and body). `to_revision`
        defaults to the current version."""
        return await get_services().knowledge.diff(
            current_principal(), id, from_revision, to_revision
        )

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_restore(id: str, revision: int, expected_revision: int) -> ChangeOut:
        """Roll an item back to what an earlier version said (title, body, kind, tags). This creates a new
        revision; the history is kept. Pass the revision you last read as `expected_revision`."""
        item = await get_services().knowledge.restore(
            current_principal(), id, revision, expected_revision
        )
        return _change_out(item)
