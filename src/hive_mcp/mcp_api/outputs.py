"""Shaping service results into the tool output models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.mcp_api.schemas import ChangeOut, ItemOut, LinkOut, MatchOut, RememberOut
from hive_mcp.services.models import Match, RememberResult, provenance

if TYPE_CHECKING:
    from hive_mcp.domain.access import Tier
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.scope import Scope

BODY_LIMIT = 20_000


def scope_label(scope: Scope) -> str:
    return f"{scope.level.name.lower()}:{scope.owner}"


def _match_out(m: Match) -> MatchOut:
    return MatchOut(
        id=m.item_id,
        title=m.title,
        snippet=m.snippet,
        kind=str(m.kind),
        tags=sorted(m.tags),
        status=str(m.status),
        score=round(m.score, 5),
        origin=m.provenance,
        external=m.tier.value == "referenced",
        scope=scope_label(m.scope),
        author=m.author_id,
        updated_at=m.updated_at.isoformat(),
        revision=m.revision,
        links=[
            LinkOut(
                relation=lk.relation,
                direction=lk.direction,
                id=lk.item_id,
                title=lk.title,
                external=lk.external,
            )
            for lk in m.links
        ],
    )


def _item_out(item: Item, tier: Tier) -> ItemOut:
    truncated = len(item.body) > BODY_LIMIT
    return ItemOut(
        id=item.id,
        title=item.title,
        body=item.body[:BODY_LIMIT],
        truncated=truncated,
        kind=str(item.kind),
        tags=sorted(item.tags),
        status=str(item.status),
        scope=scope_label(item.scope),
        visibility=str(item.visibility),
        author=item.author_id,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
        revision=item.revision,
        origin=provenance(item.scope, tier, item.author_id),
        external=tier.value == "referenced",
        has_log=bool(item.source and item.source.startswith("blob:")),
    )


def _change_out(item: Item) -> ChangeOut:
    return ChangeOut(
        id=item.id,
        revision=item.revision,
        scope=scope_label(item.scope),
        visibility=str(item.visibility),
        tags=sorted(item.tags),
        status=str(item.status),
    )


def _remember_out(r: RememberResult) -> RememberOut:
    notes = []
    if not r.created:
        notes.append("An identical earlier request was replayed; nothing new was stored.")
    if not r.indexed:
        notes.append("Stored. Search indexing is queued and will complete shortly.")
    if r.redactions:
        notes.append("Secrets were masked before storing: " + ", ".join(sorted(r.redactions)) + ".")
    return RememberOut(
        id=r.item.id,
        revision=r.item.revision,
        created=r.created,
        indexed=r.indexed,
        scope=scope_label(r.item.scope),
        redactions=r.redactions,
        message=" ".join(notes) or "Stored privately.",
    )
