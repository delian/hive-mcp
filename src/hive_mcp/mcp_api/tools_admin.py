"""Admin-only views: storage usage per person and the audit trail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, current_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


class UsageOut(BaseModel):
    user: str
    items: int
    characters: int
    last_activity: str


class PersonActivityOut(BaseModel):
    user: str
    tool_calls: int
    writes: int
    items: int
    characters: int
    last_seen: str | None
    top_tools: dict[str, int]


class ActivityOut(BaseModel):
    days: int
    people: list[PersonActivityOut]
    teams: dict[str, dict[str, int]] = Field(description="team -> {tool_calls, writes}")
    busiest_tools: dict[str, int]
    truncated: bool = Field(description="True when the window held too many events to read all.")


class AuditOut(BaseModel):
    seq: int = Field(
        description="Next page: pass the last seq as after_seq (oldest first) or before_seq (newest first)."
    )
    at: str
    actor: str
    action: str
    target: str | None
    details: dict[str, str]


def register_admin_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_admin_usage() -> list[UsageOut]:
        """Admins only: how many items and characters each person has stored (archived items excluded)."""
        rows = await get_services().admin.usage(current_principal())
        return [
            UsageOut(
                user=u.author_id,
                items=u.items,
                characters=u.chars,
                last_activity=u.last_at.isoformat(),
            )
            for u in rows
        ]

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_admin_activity(days: int = 30) -> ActivityOut:
        """Admins only: tool calls, writes and storage per person and per team over the last `days` days.

        Counts only (no content). Tool calls are saved every few minutes, so the newest are missing.
        """
        report = get_services().activity
        assert report is not None  # noqa: S101 - always built by bootstrap
        a = await report.build(current_principal(), days)
        return ActivityOut(
            days=a.days,
            people=[
                PersonActivityOut(
                    user=p.user, tool_calls=p.calls, writes=p.writes, items=p.items,
                    characters=p.characters,
                    last_seen=p.last_seen.isoformat() if p.last_seen else None,
                    top_tools=dict(p.tools.most_common(5)),
                )
                for p in a.people
            ],
            teams={t: {"tool_calls": c, "writes": w} for t, (c, w) in a.teams.items()},
            busiest_tools=dict(a.tools),
            truncated=a.truncated,
        )  # fmt: skip

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_admin_audit(  # noqa: PLR0913, PLR0917 - one parameter per filter
        actor: str | None = None,
        action: str | None = None,
        target: str | None = None,
        after_seq: int = 0,
        before_seq: int | None = None,
        newest_first: bool = False,
        limit: int = 100,
    ) -> list[AuditOut]:
        """Admins only: the audit trail, oldest first (or newest first with `newest_first`; page
        backwards by passing the last seq as `before_seq`). Filter by who (actor), what (action such as
        item.purge, promotion.approved, edge.add) or which item (target). Contains ids, never content."""
        rows = await get_services().admin.audit(
            current_principal(),
            actor=actor,
            action=action,
            target=target,
            after_seq=after_seq,
            before_seq=before_seq,
            newest_first=newest_first,
            limit=limit,
        )
        return [
            AuditOut(
                seq=r.seq,
                at=r.event.at.isoformat(),
                actor=r.event.actor_id,
                action=r.event.action,
                target=r.event.target_id,
                details=r.event.details,
            )
            for r in rows
        ]
