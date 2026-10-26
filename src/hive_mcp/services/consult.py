"""Hybrid retrieval: keyword + semantic search fused, access re-checked, ranked, origin-labelled."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.access import Tier, read_filter
from hive_mcp.domain.errors import EmbeddingUnavailable
from hive_mcp.search.ranking import RankingConfig, fuse, weight
from hive_mcp.search.snippets import snippet_for
from hive_mcp.search.tokenize import tokenize
from hive_mcp.services.consult_links import attach_links
from hive_mcp.services.models import ConsultResult, ForeignPolicy, Match, provenance

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import ItemView
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.edges import EdgeRepository
    from hive_mcp.ports.embeddings import Embedder
    from hive_mcp.ports.indexes import TextIndex, VectorIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.services.models import ConsultRequest

SNIPPET_CHARS = 400


class ConsultService:
    def __init__(  # noqa: PLR0913 - explicit dependencies
        self,
        *,
        items: ItemRepository,
        text: TextIndex,
        vectors: VectorIndex,
        embedder: Embedder,
        clock: Clock,
        ranking: RankingConfig | None = None,
        edges: EdgeRepository | None = None,
    ) -> None:
        self._edges = edges
        self._items, self._text, self._vectors = items, text, vectors
        self._embedder, self._clock, self._ranking = embedder, clock, ranking or RankingConfig()

    async def consult(self, principal: Principal, req: ConsultRequest) -> ConsultResult:
        flt, notes = read_filter(principal), []
        pool = max(req.limit * 5, 40)
        text_hits = await self._text.search(flt, req.query, pool)
        vector_hits = []
        if tokenize(req.query):
            try:
                query_vector = await self._embedder.embed_query(req.query)
                found = await self._vectors.search(flt, self._embedder.model_id, query_vector, pool)
                vector_hits = [h for h in found if h.score >= self._embedder.min_similarity]
            except EmbeddingUnavailable:
                notes.append("semantic search unavailable; showing keyword matches only")
        fused = fuse(
            [h.item_id for h in text_hits], [h.item_id for h in vector_hits], self._ranking
        )
        chunk_of = {h.item_id: h.chunk_ord for h in vector_hits}
        views = await self._items.get_many(principal, list(fused))  # authoritative access re-check
        now, scored = self._clock.now(), []
        for view in views:
            it = view.item
            if it.status not in req.statuses or (req.kinds and it.kind not in req.kinds):
                continue
            if not req.tags_all <= it.tags:
                continue
            w = weight(
                self._ranking,
                level=it.scope.level,
                tier=view.tier,
                status=it.status,
                kind=it.kind,
                updated_at=it.updated_at,
                now=now,
            )
            scored.append((fused[it.id] * w, view))
        scored.sort(key=lambda pair: (-pair[0], pair[1].item.id))
        primary = [
            self._match(s, v, chunk_of, req.query) for s, v in scored if v.tier is Tier.PRIMARY
        ]
        foreign = [
            self._match(s, v, chunk_of, req.query) for s, v in scored if v.tier is Tier.REFERENCED
        ]
        shown = primary[: req.limit]
        extra = self._foreign_slice(req, len(primary), len(shown), foreign)
        if extra and req.include_foreign is ForeignPolicy.AUTO:
            notes.append(
                f"{len(extra)} result(s) come from other groups (marked EXTERNAL) because your own "
                f"knowledge had fewer than {req.min_primary} matches"
            )
        if self._edges and req.with_links:
            linked, link_notes = await attach_links(
                principal, self._items, self._edges, [*shown, *extra]
            )
            shown, extra = linked[: len(shown)], linked[len(shown) :]
            notes.extend(link_notes)
        return ConsultResult(tuple(shown), tuple(extra), tuple(notes))

    @staticmethod
    def _foreign_slice(
        req: ConsultRequest, n_primary: int, n_shown: int, foreign: list[Match]
    ) -> list[Match]:
        if req.include_foreign is ForeignPolicy.NEVER:
            return []
        if req.include_foreign is ForeignPolicy.ALWAYS:
            return foreign[: req.limit]
        if n_primary >= req.min_primary:
            return []
        return foreign[: max(req.limit - n_shown, 2)]

    @staticmethod
    def _match(score: float, view: ItemView, chunk_of: dict[str, int], query: str) -> Match:
        it = view.item
        snippet = snippet_for(it.body, query, chunk_of.get(it.id), SNIPPET_CHARS)
        return Match(
            item_id=it.id,
            title=it.title,
            snippet=snippet,
            kind=it.kind,
            tags=it.tags,
            status=it.status,
            score=score,
            tier=view.tier,
            scope=it.scope,
            author_id=it.author_id,
            updated_at=it.updated_at,
            revision=it.revision,
            provenance=provenance(it.scope, view.tier, it.author_id),
        )
