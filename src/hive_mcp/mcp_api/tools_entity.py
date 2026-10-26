"""Entities: check whether something is already known, record it once, point notes at it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from hive_mcp.domain.entity import aliases_of
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.mcp_api.errors import guarded
from hive_mcp.mcp_api.tools import READ, WRITE, current_principal, scope_label
from hive_mcp.services.models import provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server import MCPServer

    from hive_mcp.bootstrap import Services
    from hive_mcp.domain.item import ItemView
    from hive_mcp.services.entities import EntityMatch


class EntityOut(BaseModel):
    id: str
    name: str
    type: str
    description: str
    aliases: list[str] = Field(description="Normalised names this entity answers to.")
    origin: str
    external: bool
    scope: str
    revision: int
    match: str | None = Field(
        default=None, description="exact (name/alias) or similar (close spelling)"
    )


class SaveOut(BaseModel):
    entity: EntityOut
    created: bool = Field(description="False: it was already known to you; new aliases were added.")
    added_aliases: list[str]
    conflicts: dict[str, str] = Field(description="alias -> id of the entity that already owns it")
    similar: list[EntityOut] = Field(
        description="Close spellings of other entities; check for duplicates."
    )


def _entity(view: ItemView, how: str | None = None) -> EntityOut:
    item = view.item
    kind = next((t[len("entity/") :] for t in sorted(item.tags) if t.startswith("entity/")), "")
    return EntityOut(
        id=item.id,
        name=item.title,
        type=kind,
        description=item.body,
        aliases=sorted(aliases_of(item.tags)),
        origin=provenance(item.scope, view.tier, item.author_id),
        external=view.tier.value == "referenced",
        scope=scope_label(item.scope),
        revision=item.revision,
        match=how,
    )


def _matches(found: list[EntityMatch]) -> list[EntityOut]:
    return [_entity(m.view, m.how) for m in found]


def register_entity_tools(mcp: MCPServer, get_services: Callable[[], Services]) -> None:
    @mcp.tool(annotations=READ)
    @guarded
    async def kb_entity_resolve(name: str, type: str | None = None) -> list[EntityOut]:
        """Is this person/system/part/concept already known? Matches the name and every alias, ignoring case,
        spacing and punctuation; falls back to close spellings. Call this before kb_entity_save.
        Other groups' entities appear marked external."""
        found = await get_services().entities.resolve(current_principal(), name, type)
        return _matches(found)

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_entity_save(
        name: str,
        type: str,
        description: str | None = None,
        aliases: list[str] | None = None,
        share_with_project: str | None = None,
    ) -> SaveOut:
        """Record an entity once. If it is already known to you (by name or any alias) the new aliases and
        description are added to the existing entity instead of creating a duplicate.

        `type` is free text such as person, system, component, customer, concept. Aliases are other names it
        goes by (abbreviations, former names). Check the `similar` and `conflicts` answers for near-duplicates."""
        scope = Scope(ScopeLevel.PROJECT, share_with_project) if share_with_project else None
        r = await get_services().entities.save(
            current_principal(), name, type, description, aliases, scope
        )
        return SaveOut(
            entity=_entity(r.view),
            created=r.created,
            added_aliases=r.added_aliases,
            conflicts=r.conflicts,
            similar=_matches(r.similar),
        )

    @mcp.tool(annotations=WRITE)
    @guarded
    async def kb_entity_mention(id: str, entities: list[str]) -> dict[str, str]:
        """Link a note you can edit to the entities it talks about (entity ids or names). Afterwards
        kb_graph with relations=["mentions"] on an entity lists every note that mentions it."""
        return await get_services().entities.mention(current_principal(), id, entities)
