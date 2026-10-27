"""Permanent deletion: one item, everything a departed user wrote, and age-based retention.

Archiving (kb_update status=archived) hides knowledge; purging removes it for good: the item, its
tags, search entries, vectors, links, promotion requests and (when nothing else uses it) its stored
log file. The audit trail keeps a record that it happened, with ids but no content.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import NotFound, PermissionDenied
from hive_mcp.domain.item import Kind
from hive_mcp.domain.principal import Role
from hive_mcp.domain.rules import authorize_purge
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.ports.blobs import BlobRef
from hive_mcp.ports.maintenance import ItemFilter

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.domain.item import Item
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.blobs import BlobStore
    from hive_mcp.ports.edges import EdgeRepository
    from hive_mcp.ports.indexes import TextIndex, VectorIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.jobs import Job, JobQueue
    from hive_mcp.ports.maintenance import ItemMaintenance
    from hive_mcp.ports.promotions import PromotionRepository

PURGE_JOB = "purge_cleanup"
CLEANUP_DELAY_SECONDS = 60.0
BLOB_PREFIX = "blob:"
SWEEP_KINDS = frozenset({Kind.SESSION, Kind.COMPACTION})
SYSTEM = "system:retention"
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForgetResult:
    purged: list[str] = field(default_factory=list)
    kept_shared: list[str] = field(default_factory=list)  # authored in shared scopes, left in place
    audit_events: int = 0  # events whose actor or target was replaced by an alias


class PurgeService:
    def __init__(  # noqa: PLR0913 - collaborators
        self,
        *,
        items: ItemRepository,
        maintenance: ItemMaintenance,
        text: TextIndex,
        vectors: VectorIndex,
        edges: EdgeRepository,
        promotions: PromotionRepository,
        blobs: BlobStore,
        jobs: JobQueue,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._items, self._maintenance = items, maintenance
        self._text, self._vectors, self._edges = text, vectors, edges
        self._promotions, self._blobs, self._jobs = promotions, blobs, jobs
        self._audit, self._clock = audit, clock

    async def purge(self, principal: Principal, item_id: str) -> Item:
        item = await self._items.get_system(principal.org_id, item_id)
        if item is None:
            raise NotFound(item_id)
        authorize_purge(principal, item)
        await self._purge_item(item, principal.id)
        return item

    async def purge_item(self, item: Item, actor: str) -> None:
        """Delete an item for good on behalf of `actor` (an admin or a system rule)."""
        await self._purge_item(item, actor)

    async def _purge_item(self, item: Item, actor: str) -> None:
        # The cleanup job is queued first: if we die after the row is gone, it finishes the job.
        await self._jobs.enqueue(
            item.org_id,
            PURGE_JOB,
            {"item_id": item.id, "source": item.source or ""},
            dedupe_key=f"purge:{item.id}",
            delay_seconds=CLEANUP_DELAY_SECONDS,
        )
        if await self._maintenance.purge_system(item.org_id, item.id) is None:
            return  # someone else got there first
        await self._cleanup(item.org_id, item.id, item.source)
        details = {"kind": str(item.kind), "scope": item.scope.level.name.lower()}
        await self._audit.append(
            AuditEvent(item.org_id, actor, "item.purge", self._clock.now(), item.id, details)
        )

    async def _cleanup(self, org_id: str, item_id: str, source: str | None) -> None:
        """Everything that points at a purged item. Safe to repeat."""
        await self._text.delete(item_id)
        await self._vectors.delete(item_id)
        await self._edges.remove_item(org_id, item_id)
        await self._promotions.delete_for_item(org_id, item_id)
        if source and source.startswith(BLOB_PREFIX):
            sha, _, size = source[len(BLOB_PREFIX) :].partition(":")
            if await self._maintenance.count_by_source(org_id, source) == 0:
                await self._blobs.delete(BlobRef(org_id, sha, int(size or 0)))

    async def handle_cleanup(self, job: Job) -> None:
        """Job handler for a purge interrupted after the item row was deleted."""
        item_id = job.payload["item_id"]
        if await self._items.get_system(job.org_id, item_id) is not None:
            return  # the purge never happened (died before deleting): nothing to clean
        await self._cleanup(job.org_id, item_id, job.payload.get("source") or None)

    async def forget_user(
        self,
        admin: Principal,
        user_id: str,
        *,
        include_shared: bool = False,
        dry_run: bool = True,
        erase_audit: bool = False,
    ) -> ForgetResult:
        """Erase what a person wrote: their private items always; shared ones only if asked
        (they may be valuable to the team, so by default they are listed and left)."""
        if admin.role < Role.ADMIN:
            raise PermissionDenied("only an admin can erase a user's knowledge")
        result = ForgetResult()
        after = None
        while page := await self._maintenance.find_system(
            ItemFilter(org_id=admin.org_id, author_id=user_id), after_id=after, limit=200
        ):
            after = page[-1].id
            for item in page:
                if include_shared or item.scope.level is ScopeLevel.PRIVATE:
                    result.purged.append(item.id)
                    if not dry_run:
                        await self._purge_item(item, admin.id)
                else:
                    result.kept_shared.append(item.id)
        if not dry_run:
            who = user_id
            if erase_audit:  # a random alias, so the id cannot be recovered by guessing emails
                who = f"erased-{uuid.uuid4().hex[:12]}"
                result = replace(
                    result, audit_events=await self._audit.pseudonymise(admin.org_id, user_id, who)
                )
            await self._audit.append(
                AuditEvent(
                    admin.org_id,
                    admin.id,
                    "user.forget",
                    self._clock.now(),
                    None,
                    {"user": who, "items": str(len(result.purged))},
                )
            )
        return result

    async def sweep(self, older_than_days: int, *, max_items: int = 500) -> int:
        """Retention: purge session and compaction logs older than the limit, in every org."""
        if older_than_days <= 0:
            return 0
        cutoff = self._clock.now() - timedelta(days=older_than_days)
        flt = ItemFilter(kinds=SWEEP_KINDS, created_before=cutoff)
        purged, after = 0, None
        while purged < max_items:
            page = await self._maintenance.find_system(flt, after_id=after, limit=100)
            if not page:
                break
            after = page[-1].id
            for item in page[: max_items - purged]:
                await self._purge_item(item, SYSTEM)
                purged += 1
        if purged:
            log.info("retention purged %d items older than %d days", purged, older_than_days)
        return purged
