"""Service stack over each storage backend, with the dependency-free hashing embedder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.graph import GraphService
from hive_mcp.services.indexing import IndexService
from hive_mcp.services.ingest import IngestService
from hive_mcp.services.knowledge import KnowledgeService
from hive_mcp.services.promotions import PromotionService
from hive_mcp.services.sessions import SessionService


@dataclass
class Stack:
    backend: Any
    embedder: Any
    indexing: IndexService
    knowledge: KnowledgeService
    consult: ConsultService
    sessions: SessionService
    promotions: PromotionService
    graph: GraphService
    ingest: IngestService


def build_stack(backend: Any, embedder: Any = None) -> Stack:
    embedder = embedder or HashingEmbedder(128)
    indexing = IndexService(backend.items, backend.text, backend.vectors, embedder, backend.jobs)
    knowledge = KnowledgeService(
        items=backend.items, indexing=indexing, audit=backend.audit, clock=backend.clock
    )
    consult = ConsultService(
        items=backend.items,
        text=backend.text,
        vectors=backend.vectors,
        embedder=embedder,
        clock=backend.clock,
    )
    sessions = SessionService(knowledge, backend.blobs)
    promotions = PromotionService(
        items=backend.items,
        promotions=backend.promotions,
        indexing=indexing,
        audit=backend.audit,
        clock=backend.clock,
    )
    graph = GraphService(
        items=backend.items, edges=backend.edges, audit=backend.audit, clock=backend.clock
    )
    ingest = IngestService(knowledge, graph)
    return Stack(
        backend, embedder, indexing, knowledge, consult, sessions, promotions, graph, ingest
    )


@pytest.fixture
def stack(backend: Any) -> Stack:
    return build_stack(backend)
