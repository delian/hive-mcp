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


class RateOut(BaseModel):
    recorded: bool = Field(description="False when this server does not collect search feedback.")


class SourceOut(BaseModel):
    id: str
    title: str
    opened: bool = Field(description="False: only shown in search results, never opened.")
    origin: str
    author: str
    updated_at: str
    external: bool


class MergeProposalOut(BaseModel):
    title: str
    body: str
    sources: list[str]
    flagged: bool = Field(description="The draft itself looks unsafe: read it with care.")


class ConflictOut(BaseModel):
    id: str
    title: str
    score: float
    reasons: list[str] = Field(description="Why it looks like a disagreement (heuristics).")
    model_says_contradiction: bool | None = Field(description="None: no model was asked.")
    explanation: str


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

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_rate(id: str, useful: bool, query: str | None = None) -> RateOut:
        """Say whether a search result was useful for what you were doing (true) or not (false).

        Call it after kb_consult once you know. Pass the search text as `query` if it was not your most
        recent search. Ratings tune search quality for everyone; they are visible to admins only.
        """
        svc = get_services()
        principal = current_principal()
        await svc.knowledge.get(principal, id)  # you can only rate what you can see
        if svc.signals is not None:  # counts only: one vote per person and note
            await svc.signals.record_vote(principal.org_id, principal.id, id, useful)
        if svc.feedback is not None:
            await svc.feedback.rated(principal, id, useful, query)
        return RateOut(recorded=svc.signals is not None or svc.feedback is not None)

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_session_sources(session: str) -> list[SourceOut]:
        """List the notes a work session relied on: opened ones first, then ones only shown in results.

        Uses the `session` label passed to kb_consult / kb_get (kept for a day; the stored session
        note keeps `derived_from` links to the opened notes). Review them before trusting a result:
        each carries where it came from and when it was last touched.
        """
        trail = get_services().session_sources
        assert trail is not None  # noqa: S101 - always built by bootstrap
        return [
            SourceOut(
                id=s.view.item.id, title=s.view.item.title, opened=s.opened, origin=s.origin,
                author=s.view.item.author_id, updated_at=s.view.item.updated_at.isoformat(),
                external=s.view.tier.value == "referenced",
            )
            for s in await trail.sources(current_principal(), session)
        ]  # fmt: skip

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_merge_propose(ids: list[str]) -> MergeProposalOut:
        """Ask the server's language model to draft ONE note that merges several (2 to 8).

        Nothing is saved: read the draft, fix it, then call kb_merge with the final text. Only notes
        at project level or wider are sent to the model (private ones are refused). Needs an admin to
        have configured a model; otherwise write the merged text yourself.
        """
        assist = get_services().assist
        assert assist is not None  # noqa: S101 - always built by bootstrap
        p = await assist.propose_merge(current_principal(), ids)
        return MergeProposalOut(title=p.title, body=p.body, sources=p.sources, flagged=p.flagged)

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_conflicts(id: str, limit: int = 5) -> list[ConflictOut]:
        """Find notes that may contradict this one: very similar notes whose numbers or yes/no differ
        (and, when a model is configured, that it judges to contradict). Read both, then record the
        result with kb_link relation=contradicts, or fix or retire the wrong one.
        """
        assist = get_services().assist
        assert assist is not None  # noqa: S101
        found = await assist.conflicts(current_principal(), id, limit)
        return [
            ConflictOut(
                id=c.other.view.item.id, title=c.other.view.item.title, score=round(c.score, 4),
                reasons=c.reasons, model_says_contradiction=c.model_verdict, explanation=c.explanation,
            )
            for c in found
        ]  # fmt: skip
