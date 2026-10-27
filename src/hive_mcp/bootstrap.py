"""Builds the service stack from settings (profile -> storage backend, embedder, runner)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite import SqliteBackend
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.retention import parse_rules
from hive_mcp.embeddings.factory import embed_min_level, make_embedder, make_reranker
from hive_mcp.ingest.fetch import FetchPolicy
from hive_mcp.ingest.ocr import OcrConfig, check_available
from hive_mcp.notify.factory import make_channels
from hive_mcp.ports.backend import Backend, close_backend
from hive_mcp.security.directory import UserDirectory
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services.activity import ActivityReport
from hive_mcp.services.admin import AdminService
from hive_mcp.services.consult import ConsultService
from hive_mcp.services.curation import CurationService
from hive_mcp.services.digest import DigestSender, DigestService
from hive_mcp.services.duplicates import DuplicateFinder
from hive_mcp.services.entities import EntityService
from hive_mcp.services.feedback import SearchFeedback
from hive_mcp.services.graph import GraphService
from hive_mcp.services.indexing import INDEX_JOB, IndexService
from hive_mcp.services.ingest import IngestService
from hive_mcp.services.knowledge import KnowledgeService
from hive_mcp.services.notifications import NOTIFY_JOB, NotificationService
from hive_mcp.services.promotions import APPLY_JOB, PromotionService
from hive_mcp.services.purge import PURGE_JOB, PurgeService
from hive_mcp.services.quota import CACHE_SECONDS, QuotaGuard
from hive_mcp.services.retention import RetentionService
from hive_mcp.services.runner import JobRunner
from hive_mcp.services.sessions import SessionService
from hive_mcp.services.staleness import StaleReview
from hive_mcp.services.vector_admin import VectorAdmin

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.ports.indexes import VectorIndex


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
    vector_admin: VectorAdmin
    admin: AdminService
    entities: EntityService
    digests: DigestService
    feedback: SearchFeedback | None = None
    digest_sender: DigestSender | None = None
    duplicates: DuplicateFinder | None = None
    stale: StaleReview | None = None
    retention: RetentionService | None = None
    activity: ActivityReport | None = None

    async def close(self) -> None:
        await close_backend(self.backend)


def _vector_store(settings: Settings) -> VectorIndex | None:
    if settings.vector_store == "default":
        return None
    from hive_mcp.adapters.lance.vectors import LanceVectors  # noqa: PLC0415 - optional extra

    return LanceVectors(settings.data_dir / "lance")


def _teams_of(settings: Settings) -> Callable[[str], frozenset[str]]:
    directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))

    def teams(user: str) -> frozenset[str]:
        found = next((u for u in directory.list() if u.email == user), None)
        return found.teams if found else frozenset()

    return teams


async def build_services(settings: Settings) -> Services:
    clock = SystemClock()
    backend: Backend
    if settings.profile is Profile.EMBEDDED:
        backend = SqliteBackend(settings.data_dir, clock, vectors=_vector_store(settings))
    else:
        if settings.vector_store != "default":
            raise ValidationError(
                "HIVE_VECTOR_STORE=lancedb is for the embedded profile; Postgres uses pgvector"
            )
        if not settings.database_url:
            raise ValidationError("HIVE_DATABASE_URL is required for the external profile")
        # imported lazily: the postgres extra (hive-mcp[postgres]) is optional
        from hive_mcp.adapters.postgres import PostgresBackend  # noqa: PLC0415

        backend = await PostgresBackend.connect(settings.database_url, clock=clock)
    embedder = make_embedder(settings)
    indexing = IndexService(
        backend.items,
        backend.text,
        backend.vectors,
        embedder,
        backend.jobs,
        embed_min_level(settings),
    )
    quota = QuotaGuard(
        backend.maintenance,
        max_items=settings.quota_items_per_user,
        max_chars=settings.quota_chars_per_user,
        cache_seconds=0.0 if settings.replicas > 1 else CACHE_SECONDS,
    )
    knowledge = KnowledgeService(
        items=backend.items,
        indexing=indexing,
        audit=backend.audit,
        clock=clock,
        quota=quota,
        history=backend.history,
    )
    consult = ConsultService(
        items=backend.items,
        text=backend.text,
        vectors=backend.vectors,
        embedder=embedder,
        clock=clock,
        edges=backend.edges,
        reranker=make_reranker(settings),
        rerank_depth=settings.rerank_depth,
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
    runner = JobRunner(
        backend.jobs, handlers, worker_id=f"{socket.gethostname()}-{os.getpid()}-runner"
    )
    graph = GraphService(items=backend.items, edges=backend.edges, audit=backend.audit, clock=clock)
    entities = EntityService(
        items=backend.items, text=backend.text, knowledge=knowledge, graph=graph
    )
    url_policy = (
        FetchPolicy(
            allow_private=settings.ingest_url_allow_private,
            allowed_hosts=frozenset(h.lower() for h in settings.ingest_url_hosts),
        )
        if settings.ingest_urls_enabled
        else None
    )
    ocr = None
    if settings.ocr:
        check_available()
        ocr = OcrConfig(settings.ocr_languages, settings.ocr_max_pages)
    ingest = IngestService(knowledge, graph, entities=entities, url_policy=url_policy, ocr=ocr)
    curation = CurationService(
        items=backend.items,
        vectors=backend.vectors,
        embedder=embedder,
        knowledge=knowledge,
        graph=graph,
    )
    stale = StaleReview(backend.items, backend.kv, backend.audit, clock)
    digests = DigestService(knowledge, promotions, clock, stale, settings.stale_review_days)
    email = channels.get("email")
    sender = (
        DigestSender(
            digests,
            UserDirectory(AtomicJsonFile(settings.users_path, default=[])),
            email,
            backend.kv,
            clock,
        )
        if email and settings.digest_weekday is not None
        else None
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
        VectorAdmin(backend.vectors, backend.maintenance, embedder),
        AdminService(backend.maintenance, backend.audit),
        entities,
        digests,
        SearchFeedback(backend.audit, clock) if settings.search_feedback else None,
        sender,
        DuplicateFinder(backend.items, curation),
        stale,
        RetentionService(backend.maintenance, purge, parse_rules(settings.retention_rules), clock),
        ActivityReport(backend.audit, backend.maintenance, clock, _teams_of(settings)),
    )
