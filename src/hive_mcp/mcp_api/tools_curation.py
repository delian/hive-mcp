"""Tools for tidying knowledge: bulk retag, rename tags, merge duplicates, find similar items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from hive_mcp.domain.item import Kind
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, _kinds, current_principal, scope_label
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.services.curation import BulkOutcome


class BulkOut(BaseModel):
    changed: list[str]
    unchanged: list[str]
    failed: dict[str, str] = Field(description="id -> why it could not be changed")


class MergeOut(BaseModel):
    id: str = Field(description="The new consolidated item.")
    created: bool = Field(description="False means this exact merge had already been done.")
    scope: str
    retired: list[str] = Field(description="Source items now marked superseded/archived.")
    skipped: dict[str, str]


class SimilarOut(BaseModel):
    id: str
    title: str
    score: float
    likely_duplicate: bool
    origin: str
    external: bool
    scope: str


def _bulk(out: BulkOutcome) -> BulkOut:
    return BulkOut(changed=out.changed, unchanged=out.unchanged, failed=out.failed)


def register_curation_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_retag(
        ids: list[str], add_tags: list[str] | None = None, remove_tags: list[str] | None = None
    ) -> BulkOut:
        """Add and/or remove tags on up to 100 items at once. Items you cannot edit are reported, not changed."""
        out = await get_services().curation.retag(
            current_principal(), ids, add_tags or [], remove_tags or []
        )
        return _bulk(out)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_rename_tag(old: str, new: str, dry_run: bool = True) -> BulkOut:
        """Rename a tag on every item you may edit (up to 1000). Starts as a dry run: set dry_run=false to apply."""
        out = await get_services().curation.rename_tag(
            current_principal(), old, new, dry_run=dry_run
        )
        return _bulk(out)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_merge(  # noqa: PLR0913, PLR0917 - one parameter per tool argument
        ids: list[str],
        title: str,
        body: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        retire: Literal["superseded", "archived", "keep"] = "superseded",
    ) -> MergeOut:
        """Replace 2-20 overlapping items with one consolidated item YOU write (title and body).

        Read the sources first (kb_get) and write the merged text yourself. Needs edit rights on every source.
        The new item links `supersedes` to each source; sources are marked superseded (default), archived or kept.
        Tags default to the union of the sources' tags. Retrying the same merge is safe.
        """
        r = await get_services().curation.merge(
            current_principal(),
            ids,
            title,
            body,
            kind=next(iter(_kinds([kind])), Kind.NOTE) if kind else None,
            tags=tags,
            retire=retire,
        )
        return MergeOut(
            id=r.item.id,
            created=r.created,
            scope=scope_label(r.item.scope),
            retired=r.retired,
            skipped=r.skipped,
        )

    @mcp.tool(annotations=READ)
    @guarded
    async def kb_similar(
        id: str, limit: int = 10, min_score: float | None = None
    ) -> list[SimilarOut]:
        """Find items similar in meaning to one item: spot duplicates to merge, or related notes to link.

        `likely_duplicate` marks near-identical content. Includes other groups' items, marked external.
        """
        found = await get_services().curation.similar(
            current_principal(), id, limit=limit, min_score=min_score
        )
        return [
            SimilarOut(
                id=s.view.item.id,
                title=s.view.item.title,
                score=round(s.score, 4),
                likely_duplicate=s.likely_duplicate,
                origin=provenance(s.view.item.scope, s.view.tier, s.view.item.author_id),
                external=s.view.tier.value == "referenced",
                scope=scope_label(s.view.item.scope),
            )
            for s in found
        ]
