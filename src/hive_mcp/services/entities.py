"""Entities: find out whether something is already known, record it once, point notes at it."""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from hive_mcp.domain import access
from hive_mcp.domain.access import Tier, read_filter
from hive_mcp.domain.entity import (
    ALIAS_PREFIX,
    MAX_ALIASES,
    alias_tag,
    aliases_of,
    entity_slug,
    type_tag,
)
from hive_mcp.domain.errors import HiveError, NotFound, ValidationError
from hive_mcp.domain.item import Cursor, ItemPatch, ItemQuery, ItemView, Kind
from hive_mcp.domain.text import has_bad_text
from hive_mcp.search.tokenize import tokenize
from hive_mcp.services.models import RememberRequest

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope
    from hive_mcp.ports.indexes import TextIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.graph import GraphService
    from hive_mcp.services.knowledge import KnowledgeService

SIMILAR_RATIO = 0.85
MAX_NGRAM = 4
MIN_DETECT_SLUG = 3
MAX_ENTITIES_SCANNED = 2000
MAX_MENTIONS = 20
ENTITIES = frozenset({Kind.ENTITY})


@dataclass(frozen=True, slots=True)
class EntityMatch:
    view: ItemView
    how: str  # "exact" (name or alias) or "similar" (close spelling)


@dataclass(frozen=True, slots=True)
class SaveResult:
    view: ItemView
    created: bool
    added_aliases: list[str] = field(default_factory=list)
    conflicts: dict[str, str] = field(
        default_factory=dict
    )  # alias -> id of the entity that owns it
    similar: list[EntityMatch] = field(default_factory=list)  # near-misses to double check


class EntityService:
    def __init__(
        self,
        *,
        items: ItemRepository,
        text: TextIndex,
        knowledge: KnowledgeService,
        graph: GraphService,
    ) -> None:
        self._items, self._text, self._knowledge, self._graph = items, text, knowledge, graph
        # Saving is look-up-then-create: two saves of one name at the same moment must not both
        # create it. The lock lives only while someone holds or waits for it.
        self._locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def _by_alias(
        self, principal: Principal, tag: str, entity_type: str | None, *, primary_only: bool
    ) -> list[ItemView]:
        tags = {tag} | ({type_tag(entity_type)} if entity_type else set())
        page = await self._items.list(
            principal,
            ItemQuery(
                kinds=ENTITIES,
                tags_all=frozenset(tags),
                include_referenced=not primary_only,
                limit=20,
            ),
        )
        return list(page.items)

    async def resolve(
        self, principal: Principal, name: str, entity_type: str | None = None
    ) -> list[EntityMatch]:
        """Entities known under this name or alias (yours first), else close spellings."""
        found = await self._by_alias(principal, alias_tag(name), entity_type, primary_only=False)
        if found:
            return [
                EntityMatch(v, "exact")
                for v in sorted(found, key=lambda v: v.tier is Tier.REFERENCED)
            ]
        return await self._similar(principal, name, entity_type)

    async def _similar(
        self, principal: Principal, name: str, entity_type: str | None
    ) -> list[EntityMatch]:
        slug = entity_slug(name)
        hits = await self._text.search(read_filter(principal), " ".join(slug.split("-")), 30)
        views = await self._items.get_many(principal, [h.item_id for h in hits])
        out = []
        for v in views:
            if v.item.kind is not Kind.ENTITY or v.item.status.value == "archived":
                continue
            if entity_type and type_tag(entity_type) not in v.item.tags:
                continue
            best = max(
                (SequenceMatcher(None, slug, a).ratio() for a in aliases_of(v.item.tags)), default=0
            )
            if best >= SIMILAR_RATIO:
                out.append((best, EntityMatch(v, "similar")))
        return [m for _, m in sorted(out, key=lambda p: -p[0])]

    async def save(  # noqa: PLR0913, PLR0917 - the fields of an entity
        self,
        principal: Principal,
        name: str,
        entity_type: str,
        description: str | None = None,
        aliases: list[str] | None = None,
        scope: Scope | None = None,
    ) -> SaveResult:
        """Create the entity, or - if it is already known to you - add the new aliases to it."""
        if len(aliases or []) > MAX_ALIASES:
            raise ValidationError(f"at most {MAX_ALIASES} aliases")
        key = (principal.org_id, entity_slug(name))
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        async with lock:
            return await self._save(principal, name, entity_type, description, aliases, scope)

    async def _save(  # noqa: PLR0913, PLR0917 - the fields of an entity
        self,
        principal: Principal,
        name: str,
        entity_type: str,
        description: str | None,
        aliases: list[str] | None,
        scope: Scope | None,
    ) -> SaveResult:
        tags = list(dict.fromkeys(alias_tag(n) for n in [name, *(aliases or [])]))
        # Only the name decides "same entity". An alias that already belongs to another entity is a
        # conflict to report, never a reason to merge two things that may differ.
        existing = await self._find_own(principal, tags[:1], entity_type)
        owners = await self._owners(principal, tags)
        keep = existing.item.id if existing else None
        conflicts = {
            t[len(ALIAS_PREFIX) :]: other
            for t in tags
            if (other := owners.get(t)) and other != keep
        }
        if existing is None:
            if tags[0][len(ALIAS_PREFIX) :] in conflicts:
                other = conflicts[tags[0][len(ALIAS_PREFIX) :]]
                raise ValidationError(
                    f"{name!r} already belongs to entity {other}, which you cannot edit; "
                    "link your notes to it instead"
                )
            tags = [t for t in tags if t[len(ALIAS_PREFIX) :] not in conflicts]
            req = RememberRequest(
                title=name.strip(),
                body=(description or f"{entity_type}: {name.strip()}").strip(),
                kind=Kind.ENTITY,
                tags=(type_tag(entity_type), *tags),
                scope=scope,
            )
            created = await self._knowledge.remember(principal, req)
            view = await self._knowledge.get(principal, created.item.id)
            near = [
                m
                for m in await self._near(principal, name, entity_type)
                if m.view.item.id != view.item.id
            ]
            return SaveResult(view, True, [t[len(ALIAS_PREFIX) :] for t in tags], conflicts, near)
        new_tags = [
            t
            for t in tags
            if t not in existing.item.tags and t[len(ALIAS_PREFIX) :] not in conflicts
        ]
        body = description.strip() if description and description.strip() else None
        body = body if body != existing.item.body else None
        if new_tags or body:
            patch = ItemPatch(add_tags=frozenset(new_tags), body=body)
            await self._knowledge.update(principal, existing.item.id, patch, existing.item.revision)
            existing = await self._knowledge.get(principal, existing.item.id)
        return SaveResult(existing, False, [t[len(ALIAS_PREFIX) :] for t in new_tags], conflicts)

    async def _find_own(
        self, principal: Principal, tags: list[str], entity_type: str | None
    ) -> ItemView | None:
        for tag in tags:
            for view in await self._by_alias(principal, tag, entity_type, primary_only=True):
                if access.can_write(principal, view.item.acl, view.item.author_id):
                    return view
        return None

    async def _owners(self, principal: Principal, tags: list[str]) -> dict[str, str]:
        owners: dict[str, str] = {}
        for tag in tags:
            for view in await self._by_alias(principal, tag, None, primary_only=True):
                owners.setdefault(tag, view.item.id)
        return owners

    async def _near(
        self, principal: Principal, name: str, entity_type: str | None
    ) -> list[EntityMatch]:
        return [m for m in await self._similar(principal, name, entity_type)][:3]

    async def mention(
        self, principal: Principal, item_id: str, entities: list[str]
    ) -> dict[str, str]:
        """Link an item to entities (ids or names). Returns each input -> 'linked' or why not."""
        out: dict[str, str] = {}
        for ref in dict.fromkeys(entities):
            try:
                target = await self._resolve_ref(principal, ref)
                created = await self._graph.link(principal, item_id, target.item.id, "mentions")
                out[ref] = "linked" if created else "already linked"
            except HiveError as exc:
                out[ref] = "not found" if isinstance(exc, NotFound) else str(exc)
        return out

    async def _resolve_ref(self, principal: Principal, ref: str) -> ItemView:
        if has_bad_text(ref):
            raise NotFound(ref)
        try:
            view = await self._items.get(principal, ref)
            if view.item.kind is Kind.ENTITY:
                return view
        except NotFound:
            pass
        matches = [m for m in await self.resolve(principal, ref) if m.how == "exact"]
        if not matches:
            raise NotFound(ref)
        return matches[0].view

    async def detect_mentions(self, principal: Principal, text: str) -> list[str]:
        """Ids of the caller's own entities whose name or alias occurs in `text`."""
        words = tokenize(text)
        grams = {
            "-".join(words[i : i + n])
            for n in range(1, MAX_NGRAM + 1)
            for i in range(len(words) - n + 1)
        }
        found: list[str] = []
        scanned, after = 0, None
        while scanned < MAX_ENTITIES_SCANNED:
            page = await self._items.list(
                principal,
                ItemQuery(kinds=ENTITIES, include_referenced=False, limit=100, after=after),
            )
            for view in page.items:
                scanned += 1
                slugs = {s for s in aliases_of(view.item.tags) if len(s) >= MIN_DETECT_SLUG}
                if slugs & grams:
                    found.append(view.item.id)
            if not page.next_cursor:
                break
            after = Cursor.decode(page.next_cursor)
        return found[:MAX_MENTIONS]


__all__ = ["EntityMatch", "EntityService", "SaveResult"]
