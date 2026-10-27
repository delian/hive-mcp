"""Admin-only views: storage usage per person and the audit trail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.integrations.config import IntegrationConfig


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


class IntegrationOut(BaseModel):
    name: str
    enabled: bool
    workspace: str = Field(description="Slack workspace id, or the Teams (Entra) tenant id.")
    app_id: str = Field(description="Teams bot application id (empty for Slack).")
    allow_mentions: bool
    channel_projects: dict[str, str] = Field(
        description="channel id -> project that 'Save to Hive' shares into (members only)."
    )
    users: dict[str, str] = Field(
        description="chat user id -> work email (used without a bot token)."
    )
    secrets_set: list[str] = Field(
        description="Which secrets are stored. Their values are never shown or accepted here."
    )


class AuditOut(BaseModel):
    seq: int = Field(
        description="Next page: pass the last seq as after_seq (oldest first) or before_seq (newest first)."
    )
    at: str
    actor: str
    action: str
    target: str | None
    details: dict[str, str]


def _require_admin() -> None:
    if current_principal().role < Role.ADMIN:
        raise PermissionDenied("this needs an admin")


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

    def _integration_out(config: IntegrationConfig) -> IntegrationOut:
        return IntegrationOut(**config.public())

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_admin_integration(name: str) -> IntegrationOut:
        """Admins only: the settings of a chat integration ("slack" or "teams"), secrets excluded."""
        store = get_services().integrations
        assert store is not None  # noqa: S101 - set by the app
        current_principal()  # authenticated
        _require_admin()
        return _integration_out(await store.get(name))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_admin_integration_set(  # noqa: PLR0913, PLR0917 - one parameter per setting
        name: str,
        enabled: bool | None = None,
        workspace: str | None = None,
        app_id: str | None = None,
        allow_mentions: bool | None = None,
        users: dict[str, str] | None = None,
        signing_secret: str | None = None,
        bot_token: str | None = None,
        app_password: str | None = None,
    ) -> IntegrationOut:
        """Admins only: change a chat integration ("slack" or "teams"); only what you pass changes.

        Takes effect within seconds, no restart. Enabling needs the workspace id and the secrets;
        **secrets (signing secret, bot token, app password) cannot be set here**, because tool
        arguments are visible in chat transcripts: use the admin page (/admin/integrations) or the
        HIVE_SLACK_* variables. Every change is audited.
        """
        store = get_services().integrations
        assert store is not None  # noqa: S101
        if signing_secret or bot_token or app_password:  # refuse loudly rather than ignore
            raise ValidationError(
                "secrets cannot be set through chat (tool arguments appear in transcripts): "
                "use the admin page /admin/integrations or the HIVE_SLACK_* variables"
            )
        changes = {
            k: v
            for k, v in (
                ("enabled", enabled), ("workspace", workspace), ("app_id", app_id),
                ("allow_mentions", allow_mentions), ("users", users),
            )
            if v is not None
        }  # fmt: skip
        return _integration_out(await store.update(current_principal(), name, changes))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_admin_integration_map(
        name: str, channel: str, project: str = ""
    ) -> IntegrationOut:
        """Admins only: make a chat channel share into a project ("Save to Hive" there saves to the
        project for its members instead of privately). Empty `project` removes the mapping."""
        store = get_services().integrations
        assert store is not None  # noqa: S101
        who = current_principal()
        current = (await store.get(name)).channel_projects
        mapping = {k: v for k, v in current.items() if k != channel}
        if project:
            mapping[channel] = project
        return _integration_out(await store.update(who, name, {"channel_projects": mapping}))

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
