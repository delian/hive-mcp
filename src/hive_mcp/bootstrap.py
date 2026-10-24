"""Builds the service stack from settings (profile -> storage backend, embedder, runner)."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite import SqliteBackend
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.hashing import HashingEmbedder
from hive_mcp.ports.backend import Backend, close_backend
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.indexing import INDEX_JOB, IndexService
from hive_mcp.services.knowledge import KnowledgeService
from hive_mcp.services.runner import JobRunner
from hive_mcp.services.sessions import SessionService

if TYPE_CHECKING:
    from hive_mcp.ports.embeddings import Embedder


@dataclass
class Services:
    knowledge: KnowledgeService
    consult: ConsultService
    sessions: SessionService
    indexing: IndexService
    runner: JobRunner
    backend: Backend

    async def close(self) -> None:
        await close_backend(self.backend)


def make_embedder(name: str) -> Embedder:
    if name == "hashing":
        return HashingEmbedder(256)
    raise ValidationError(f"unknown embedder {name!r} (available: hashing)")


async def build_services(settings: Settings) -> Services:
    clock = SystemClock()
    backend: Backend
    if settings.profile is Profile.EMBEDDED:
        backend = SqliteBackend(settings.data_dir, clock)
    else:
        if not settings.database_url:
            raise ValidationError("HIVE_DATABASE_URL is required for the external profile")
        # imported lazily: the postgres extra (hive-mcp[postgres]) is optional
        from hive_mcp.adapters.postgres import PostgresBackend  # noqa: PLC0415

        backend = await PostgresBackend.connect(
            settings.database_url, settings.data_dir / "blobs", clock=clock
        )
    embedder = make_embedder(settings.embedder)
    indexing = IndexService(backend.items, backend.text, backend.vectors, embedder, backend.jobs)
    knowledge = KnowledgeService(
        items=backend.items, indexing=indexing, audit=backend.audit, clock=clock
    )
    consult = ConsultService(
        items=backend.items,
        text=backend.text,
        vectors=backend.vectors,
        embedder=embedder,
        clock=clock,
    )
    sessions = SessionService(knowledge, backend.blobs)
    handlers = {INDEX_JOB: indexing.handle_job}
    runner = JobRunner(backend.jobs, handlers, worker_id=f"{socket.gethostname()}-runner")
    return Services(knowledge, consult, sessions, indexing, runner, backend)
