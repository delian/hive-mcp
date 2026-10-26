"""Graph-aware answers: show how each result connects to other knowledge the caller can see.

Adds each result's links (capped), and notes for the two situations an agent must not miss:
a result that a newer item supersedes, and results that contradict each other.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hive_mcp.domain.access import Tier
from hive_mcp.domain.item import Status
from hive_mcp.services.models import Match, MatchLink

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hive_mcp.domain.item import ItemView
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.edges import EdgeRepository
    from hive_mcp.ports.items import ItemRepository

MAX_LINKS_PER_RESULT = 5
MAX_NOTES = 5
EDGE_BUDGET = 200
# Closest-meaning relations first, so the cap keeps the most useful links.
RELATION_ORDER = ("supersedes", "contradicts", "derived_from", "supports", "part_of", "next")


def _rank(link: MatchLink) -> tuple[int, str, str]:
    rel = RELATION_ORDER.index(link.relation) if link.relation in RELATION_ORDER else 99
    return rel, link.relation, link.item_id


async def attach_links(
    principal: Principal,
    items: ItemRepository,
    edges: EdgeRepository,
    matches: Sequence[Match],
) -> tuple[list[Match], list[str]]:
    """Returns the matches with `links` filled in, plus warnings. Only items the caller can see
    (and that are not archived) are ever mentioned."""
    if not matches:
        return [], []
    shown = {m.item_id: m for m in matches}
    found = await edges.incident(principal.org_id, list(shown), limit=EDGE_BUDGET)
    others = {e.dst if e.src in shown else e.src for e in found}
    visible: dict[str, ItemView] = {
        v.item.id: v
        for v in await items.get_many(principal, sorted(others))
        if v.item.status is not Status.ARCHIVED
    }
    per_match: dict[str, list[MatchLink]] = {i: [] for i in shown}
    notes: list[str] = []
    for edge in found:
        for here, direction, there in ((edge.src, "out", edge.dst), (edge.dst, "in", edge.src)):
            if here not in shown:
                continue
            seen = _described(visible.get(there), shown.get(there))
            if seen is not None:
                per_match[here].append(MatchLink(edge.relation, direction, there, *seen))
    for match in matches:
        for link in per_match[match.item_id]:
            notes.extend(_warnings(match, link, shown))
    result = [
        replace(m, links=tuple(sorted(set(per_match[m.item_id]), key=_rank)[:MAX_LINKS_PER_RESULT]))
        for m in matches
    ]
    return result, list(dict.fromkeys(notes))[:MAX_NOTES]


def _described(view: ItemView | None, match: Match | None) -> tuple[str, bool] | None:
    """(title, is_external) of a linked item the caller can see, else None."""
    if view is not None:
        return view.item.title, view.tier is Tier.REFERENCED
    if match is not None:
        return match.title, match.tier is Tier.REFERENCED
    return None


def _warnings(match: Match, link: MatchLink, shown: dict[str, Match]) -> list[str]:
    if link.relation == "supersedes" and link.direction == "in":
        extra = "" if link.item_id in shown else " (not among these results; read it first)"
        return [f'"{match.title}" has been superseded by "{link.title}" [{link.item_id}]{extra}']
    if link.relation == "contradicts":
        return [
            f'"{match.title}" and "{link.title}" contradict each other: '
            "reconcile them before relying on either"
        ]
    return []
