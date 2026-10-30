"""Tools for other groups' notes: adopt one, hide one, propose a change to one."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.outputs import _remember_out
from hive_mcp.mcp_api.schemas import RememberOut  # noqa: TC001 - the tool framework reads it at run time
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.services.foreign import ForeignService, Suggestion, UpstreamDiff


class DiffOut(BaseModel):
    upstream_id: str
    forked_revision: int
    upstream_revision: int | None = Field(description="None: the original is gone or hidden.")
    identical: bool
    diff: str = Field(description="Unified diff, your fork against the original now.")
    truncated: bool


class IgnoreOut(BaseModel):
    id: str
    hidden: bool


class SuggestionOut(BaseModel):
    id: str
    item_id: str
    item_title: str
    item_revision: int = Field(description="The revision the proposal was written against.")
    proposed_by: str
    new_title: str | None
    new_body: str | None
    note: str
    status: str
    created_at: str


def _diff_out(d: UpstreamDiff) -> DiffOut:
    return DiffOut(
        upstream_id=d.upstream_id, forked_revision=d.forked_revision,
        upstream_revision=d.upstream_revision, identical=d.identical, diff=d.diff,
        truncated=d.truncated,
    )  # fmt: skip


def _suggestion_out(s: Suggestion) -> SuggestionOut:
    return SuggestionOut(
        id=s.id, item_id=s.item_id, item_title=s.item_title, item_revision=s.item_revision,
        proposed_by=s.proposer, new_title=s.title, new_body=s.body, note=s.note,
        status=s.status, created_at=s.created_at,
    )  # fmt: skip


def register_foreign_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    def foreign() -> ForeignService:
        service = get_services().foreign
        assert service is not None  # noqa: S101 - built by bootstrap
        return service

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_fork(id: str, share_with_project: str | None = None) -> RememberOut:
        """Adopt another group's note: copy it into your own scope (private, or a project you belong to) and link it `derived_from` the original.

        Use this when you want to build on a note marked external instead of relying on it as it
        is. The copy is yours to edit; kb_diff_upstream shows how the original moves on.
        """
        scope = Scope(ScopeLevel.PROJECT, share_with_project) if share_with_project else None
        return _remember_out(await foreign().fork(current_principal(), id, scope))

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_diff_upstream(id: str) -> DiffOut:
        """For a note made with kb_fork: what differs between your copy and the original as it is now."""
        return _diff_out(await foreign().diff_upstream(current_principal(), id))

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_ignore(id: str, hide: bool = True) -> IgnoreOut:
        """Stop another group's note from showing up in YOUR search results (hide=false brings it back).

        Only for notes marked external; nobody else's results change. To retire a note of your own
        use kb_update with status=archived.
        """
        await foreign().ignore(current_principal(), id, hide)
        return IgnoreOut(id=id, hidden=hide)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_suggest_edit(
        id: str, new_title: str | None = None, new_body: str | None = None, note: str = ""
    ) -> SuggestionOut:
        """Propose a better title and/or body for a note you cannot edit; the people who can edit it review it (kb_list_suggestions).

        Give the full replacement text, and `note` saying why. Secrets are masked. Suggesting again
        replaces your earlier open suggestion for the same note.
        """
        s = await foreign().suggest_edit(
            current_principal(), id, title=new_title, body=new_body, note=note
        )
        return _suggestion_out(s)

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_list_suggestions(include_decided: bool = False) -> list[SuggestionOut]:
        """Edit suggestions waiting for you, on notes you may edit. Newest first."""
        found = await foreign().list_suggestions(current_principal(), include_decided)
        return [_suggestion_out(s) for s in found]

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_decide_suggestion(id: str, decision: Literal["accept", "reject"]) -> SuggestionOut:
        """Accept (applies the proposed text to the note) or reject an edit suggestion.

        Accepting only works while the note is still at the revision the proposer saw.
        """
        s = await foreign().decide_suggestion(current_principal(), id, decision)
        return _suggestion_out(s)
