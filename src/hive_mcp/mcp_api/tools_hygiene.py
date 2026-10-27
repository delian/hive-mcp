"""Tools that keep the knowledge base trustworthy over time: duplicate sweeps, stale review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal, scope_label
from hive_mcp.services.staleness import age_days

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services


class DuplicateGroupOut(BaseModel):
    ids: list[str] = Field(
        description="Items that say (nearly) the same thing; merge with kb_merge."
    )
    best_score: float
    exact: bool = Field(description="True when the text is identical, not just close in meaning.")


class DuplicateSweepOut(BaseModel):
    groups: list[DuplicateGroupOut]
    scanned: int
    next_cursor: str | None = Field(description="Pass as `after` to continue; null when complete.")


class StaleItemOut(BaseModel):
    id: str
    title: str
    kind: str
    scope: str
    last_touched: str = Field(description="Last edit or confirmation, whichever is later.")
    age_days: int
    confirmed_by: str | None = Field(description="Who last vouched for it, if anyone.")


class StaleOut(BaseModel):
    items: list[StaleItemOut]
    next_cursor: str | None = Field(description="Pass as `after` to continue; null when complete.")


class ConfirmOut(BaseModel):
    id: str
    confirmed_at: str


def register_hygiene_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_duplicates(
        after: str | None = None, scan: int = 50, threshold: float = 0.9
    ) -> DuplicateSweepOut:
        """Find groups of near-duplicate notes among the items you can edit.

        Scans `scan` items per call (continue with `next_cursor`). Read each group (kb_get), write one
        consolidated note, and call kb_merge with the ids; nothing is changed by this tool.
        """
        finder = get_services().duplicates
        assert finder is not None  # noqa: S101 - always built by bootstrap
        sweep = await finder.sweep(current_principal(), after=after, scan=scan, threshold=threshold)
        return DuplicateSweepOut(
            groups=[
                DuplicateGroupOut(ids=list(g.ids), best_score=round(g.best_score, 4), exact=g.exact)
                for g in sweep.groups
            ],
            scanned=sweep.scanned,
            next_cursor=sweep.next_cursor,
        )

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_review_stale(
        days: int = 180, limit: int = 30, after: str | None = None, mine_only: bool = True
    ) -> StaleOut:
        """List notes nobody has edited or confirmed for `days` days: candidates for "is this still true?".

        Default: your own notes; mine_only=false lists any note you can edit (for curators). For each
        one, read it (kb_get), then keep it (kb_confirm), fix it (kb_update) or retire it
        (kb_update status=archived, or superseded when a newer note replaces it).
        """
        svc = get_services().stale
        assert svc is not None  # noqa: S101 - always built by bootstrap
        now = get_services().digests.now()
        page = await svc.stale(
            current_principal(), days=days, limit=limit, after=after, mine_only=mine_only
        )
        return StaleOut(
            items=[
                StaleItemOut(
                    id=s.item.id, title=s.item.title, kind=str(s.item.kind),
                    scope=scope_label(s.item.scope), last_touched=s.last_touched.isoformat(),
                    age_days=age_days(s.last_touched, now), confirmed_by=s.confirmed_by,
                )
                for s in page.items
            ],
            next_cursor=page.next_cursor,
        )  # fmt: skip

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_confirm(id: str) -> ConfirmOut:
        """Vouch that a note is still true. Resets its staleness clock without changing its text.

        Only for notes you can edit, and only after you have read them: confirming something
        unverified makes stale knowledge look fresh.
        """
        svc = get_services().stale
        assert svc is not None  # noqa: S101
        at = await svc.confirm(current_principal(), id)
        return ConfirmOut(id=id, confirmed_at=at.isoformat())
