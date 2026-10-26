"""Keeping the knowledge base tidy: bulk retag, tag rename, merging duplicates, similar items.

The server does no summarising itself (it has no model): the calling agent writes the merged text,
and these methods make the change safely (access checks, revisions, links, audit).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from hive_mcp.domain import access
from hive_mcp.domain.errors import HiveError, NotFound, PermissionDenied, ValidationError
from hive_mcp.domain.item import Cursor, Item, ItemPatch, ItemQuery, ItemView, Kind, Status
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.services.indexing import embedding_texts
from hive_mcp.services.knowledge import can_share_directly
from hive_mcp.services.models import RememberRequest

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.embeddings import Embedder
    from hive_mcp.ports.indexes import VectorIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.graph import GraphService
    from hive_mcp.services.knowledge import KnowledgeService

MAX_BULK = 100
MAX_RENAME = 1000
MAX_MERGE = 20
DUPLICATE_SCORE = 0.9
ALL_STATUSES = frozenset(Status)
RetireAs = Literal["superseded", "archived", "keep"]


@dataclass(frozen=True, slots=True)
class BulkOutcome:
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # id -> reason


@dataclass(frozen=True, slots=True)
class MergeResult:
    item: Item
    created: bool
    retired: list[str]
    skipped: dict[str, str]


@dataclass(frozen=True, slots=True)
class Similar:
    view: ItemView
    score: float
    likely_duplicate: bool


class CurationService:
    def __init__(
        self,
        *,
        items: ItemRepository,
        vectors: VectorIndex,
        embedder: Embedder,
        knowledge: KnowledgeService,
        graph: GraphService,
    ) -> None:
        self._items, self._vectors, self._embedder = items, vectors, embedder
        self._knowledge, self._graph = knowledge, graph

    async def _retag_one(
        self, principal: Principal, view: ItemView, add: frozenset[str], remove: frozenset[str]
    ) -> bool:
        item = view.item
        if (item.tags | add) - remove == item.tags:
            return False
        patch = ItemPatch(add_tags=add - remove, remove_tags=remove & item.tags)
        await self._knowledge.update(principal, item.id, patch, item.revision)
        return True

    async def retag(
        self, principal: Principal, ids: list[str], add: list[str], remove: list[str]
    ) -> BulkOutcome:
        """Apply the same tag changes to many items; one failure never blocks the others."""
        if not 1 <= len(ids) <= MAX_BULK:
            raise ValidationError(f"give between 1 and {MAX_BULK} ids")
        add_t, remove_t = normalize_tags(add), normalize_tags(remove)
        if add_t & remove_t or not (add_t or remove_t):
            raise ValidationError("name tags to add or remove (not the same tag in both)")
        out = BulkOutcome()
        for item_id in dict.fromkeys(ids):
            try:
                view = await self._items.get(principal, item_id)
                done = await self._retag_one(principal, view, add_t, remove_t)
                (out.changed if done else out.unchanged).append(item_id)
            except HiveError as exc:
                out.failed[item_id] = "not found" if isinstance(exc, NotFound) else str(exc)
        return out

    async def rename_tag(
        self, principal: Principal, old: str, new: str, *, dry_run: bool = False
    ) -> BulkOutcome:
        """Rename a tag on every item the caller can edit (others are reported, not touched)."""
        (old_t,), (new_t,) = normalize_tags([old]), normalize_tags([new])
        if old_t == new_t:
            raise ValidationError("old and new tag are the same")
        out, cursor = BulkOutcome(), None
        query = ItemQuery(
            tags_all=frozenset({old_t}), statuses=ALL_STATUSES, include_referenced=False, limit=100
        )
        seen = 0
        while seen < MAX_RENAME:
            page = await self._items.list(principal, query)
            for view in page.items:
                seen += 1
                item = view.item
                if not access.can_write(principal, item.acl, item.author_id):
                    out.failed[item.id] = "you cannot edit this item"
                elif dry_run:
                    out.changed.append(item.id)
                else:
                    try:
                        await self._retag_one(
                            principal, view, frozenset({new_t}), frozenset({old_t})
                        )
                        out.changed.append(item.id)
                    except HiveError as exc:
                        out.failed[item.id] = str(exc)
            cursor = page.next_cursor
            if not cursor:
                break
            query = replace(query, after=Cursor.decode(cursor))
        return out

    async def merge(  # noqa: PLR0913 - mirrors the tool
        self,
        principal: Principal,
        source_ids: list[str],
        title: str,
        body: str,
        *,
        kind: Kind | None = None,
        tags: list[str] | None = None,
        retire: RetireAs = "superseded",
    ) -> MergeResult:
        """Replace several items by one consolidated item written by the caller.

        The new item links `supersedes` to each source; sources are marked superseded (or archived).
        Retrying the same merge returns the same item."""
        ids = list(dict.fromkeys(source_ids))
        if not 2 <= len(ids) <= MAX_MERGE:  # noqa: PLR2004
            raise ValidationError(f"merge between 2 and {MAX_MERGE} distinct items")
        views = [await self._items.get(principal, i) for i in ids]
        for view in views:
            if not access.can_write(principal, view.item.acl, view.item.author_id):
                raise PermissionDenied(f"you cannot edit {view.item.id}")
        first = views[0].item
        same_place = all(
            v.item.scope == first.scope and v.item.visibility == first.visibility for v in views
        )
        if same_place and can_share_directly(principal, first.scope, first.visibility):
            scope, visibility = first.scope, first.visibility
        else:
            scope, visibility = Scope(ScopeLevel.PRIVATE, principal.id), Visibility.SCOPE
        merged_tags = (  # explicit tags win, else the sources' union
            normalize_tags(tags)
            if tags is not None
            else frozenset().union(*(v.item.tags for v in views))
        )
        fingerprint = hashlib.sha256(
            "\0".join([*ids, title, body, str(kind or first.kind), *sorted(merged_tags)]).encode()
        ).hexdigest()
        saved = await self._knowledge.remember(
            principal,
            RememberRequest(
                title=title,
                body=body,
                kind=kind or first.kind,
                tags=tuple(sorted(merged_tags)),
                scope=scope,
                visibility=visibility,
                idempotency_key=f"merge:{fingerprint}",
            ),
        )
        retired: list[str] = []
        skipped: dict[str, str] = {}
        for view in views:
            try:
                await self._graph.link(principal, saved.item.id, view.item.id, "supersedes")
                if retire != "keep":
                    status = Status.SUPERSEDED if retire == "superseded" else Status.ARCHIVED
                    current = await self._items.get(principal, view.item.id)
                    if current.item.status is not status:
                        await self._knowledge.update(
                            principal, view.item.id, ItemPatch(status=status), current.item.revision
                        )
                    retired.append(view.item.id)
            except HiveError as exc:
                skipped[view.item.id] = str(exc)
        return MergeResult(saved.item, saved.created, retired, skipped)

    async def similar(
        self,
        principal: Principal,
        item_id: str,
        *,
        limit: int = 10,
        min_score: float | None = None,
    ) -> list[Similar]:
        """Items most like this one (by meaning), for spotting duplicates and related notes."""
        if not 1 <= limit <= 50:  # noqa: PLR2004
            raise ValidationError("limit must be between 1 and 50")
        view = await self._items.get(principal, item_id)
        texts = embedding_texts(view.item)
        if not texts:
            return []
        (vector,) = await self._embedder.embed(texts[:1])
        floor = self._embedder.min_similarity if min_score is None else min_score
        hits = await self._vectors.search(
            access.read_filter(principal), self._embedder.model_id, vector, limit + 1
        )
        scores = {h.item_id: h.score for h in hits if h.item_id != item_id and h.score >= floor}
        found = await self._items.get_many(principal, list(scores))
        result = [
            Similar(
                v,
                scores[v.item.id],
                scores[v.item.id] >= DUPLICATE_SCORE
                or v.item.content_hash == view.item.content_hash,
            )
            for v in found
            if v.item.status is not Status.ARCHIVED
        ]
        result.sort(key=lambda s: -s.score)
        return result[:limit]
