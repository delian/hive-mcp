"""Builds the service stack from settings (profile -> storage backend, embedder, runner)."""

from __future__ import annotations

import socket
from dataclasses import dataclass

from hive_mcp.adapters.sqlite import SqliteBackend
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import ValidationError
from hive_mcp.embeddings.factory import embed_min_level, make_embedder
from hive_mcp.notify.factory import make_channels
from hive_mcp.ports.backend import Backend, close_backend
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.curation import CurationService
from hive_mcp.services.graph import GraphService
from hive_mcp.services.indexing import INDEX_JOB, IndexService
from hive_mcp.services.ingest import IngestService
from hive_mcp.services.knowledge import KnowledgeService
from hive_mcp.services.notifications import NOTIFY_JOB, NotificationService
from hive_mcp.services.promotions import APPLY_JOB, PromotionService
from hive_mcp.services.purge import PURGE_JOB, PurgeService
from hive_mcp.services.runner import JobRunner
from hive_mcp.services.sessions import SessionService


@dataclass
class Services:
    knowledge: KnowledgeService
    consult: ConsultService
    sessions: SessionService
    indexing: IndexService
    runner: JobRunner
    backend: Backend
    promotions: PromotionService
    graph: GraphService
    ingest: IngestService
    curation: CurationService
    purge: PurgeService

    async def close(self) -> None:
        await close_backend(self.backend)


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
    embedder = make_embedder(settings)
    indexing = IndexService(
        backend.items,
        backend.text,
        backend.vectors,
        embedder,
        backend.jobs,
        embed_min_level(settings),
    )
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
    channels = make_channels(settings)
    notifications = (
        NotificationService(
            channels=channels,
            promotions=backend.promotions,
            items=backend.items,
            directory=UserDirectory(AtomicJsonFile(settings.users_path, default=[])),
            jobs=backend.jobs,
        )
        if channels
        else None
    )
    promotions = PromotionService(
        items=backend.items,
        promotions=backend.promotions,
        indexing=indexing,
        jobs=backend.jobs,
        audit=backend.audit,
        clock=clock,
        notifications=notifications,
    )
    handlers = {INDEX_JOB: indexing.handle_job, APPLY_JOB: promotions.handle_apply}
    if notifications:
        handlers[NOTIFY_JOB] = notifications.handle
    purge = PurgeService(
        items=backend.items,
        maintenance=backend.maintenance,
        text=backend.text,
        vectors=backend.vectors,
        edges=backend.edges,
        promotions=backend.promotions,
        blobs=backend.blobs,
        jobs=backend.jobs,
        audit=backend.audit,
        clock=clock,
    )
    handlers[PURGE_JOB] = purge.handle_cleanup
    runner = JobRunner(backend.jobs, handlers, worker_id=f"{socket.gethostname()}-runner")
    graph = GraphService(items=backend.items, edges=backend.edges, audit=backend.audit, clock=clock)
    ingest = IngestService(knowledge, graph)
    curation = CurationService(
        items=backend.items,
        vectors=backend.vectors,
        embedder=embedder,
        knowledge=knowledge,
        graph=graph,
    )
    return Services(
        knowledge,
        consult,
        sessions,
        indexing,
        runner,
        backend,
        promotions,
        graph,
        ingest,
        curation,
        purge,
    )
