"""Keeps text and vector indexes in step with items. Failures queue a job; items are never lost."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.item import Status
from hive_mcp.ingest.chunker import chunk_text
from hive_mcp.ports.indexes import TextDoc
from hive_mcp.search.tokenize import tokenize

if TYPE_CHECKING:
    from hive_mcp.domain.item import Item
    from hive_mcp.ports.embeddings import Embedder
    from hive_mcp.ports.indexes import TextIndex, VectorIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.jobs import Job, JobQueue

INDEX_JOB = "index_item"
MAX_REINDEX_ROUNDS = 3


def embedding_texts(item: Item) -> list[str]:
    """What gets embedded: each body chunk prefixed with the title (chunk ordinal == position)."""
    return [f"{item.title}\n\n{c}" for c in chunk_text(item.body)]


class IndexService:
    def __init__(
        self,
        items: ItemRepository,
        text: TextIndex,
        vectors: VectorIndex,
        embedder: Embedder,
        jobs: JobQueue,
    ) -> None:
        self._items, self._text, self._vectors = items, text, vectors
        self._embedder, self._jobs = embedder, jobs

    async def index_item(self, item: Item) -> None:
        """Make the indexes match `item` exactly (archived items are removed)."""
        if item.status is Status.ARCHIVED:
            await self._text.delete(item.id)
            await self._vectors.delete(item.id)
            return
        body = f"{item.title}\n{item.body}\n{' '.join(sorted(item.tags))}"
        await self._text.upsert(TextDoc(item.id, item.acl, body))
        texts = embedding_texts(item)
        usable = next((t for t in texts if tokenize(t)), None)
        if usable is None:  # no words anywhere: findable by id only, nothing to embed
            await self._vectors.replace(item.id, self._embedder.model_id, item.acl, [])
            return
        # A wordless chunk ("???") cannot be embedded: reuse another text, keeping ordinals aligned.
        texts = [t if tokenize(t) else usable for t in texts]
        vectors = await self._embedder.embed(texts)
        await self._vectors.replace(item.id, self._embedder.model_id, item.acl, vectors)

    async def index_current(self, org_id: str, item_id: str) -> bool:
        """Index the latest stored version; repeats if the item changes meanwhile."""
        for _ in range(MAX_REINDEX_ROUNDS):
            item = await self._items.get_system(org_id, item_id)
            if item is None:
                # Missing in this org. Items are archived, never hard-deleted, so there is nothing
                # to clean up, and deleting by id could touch another organisation's entries.
                return True
            await self.index_item(item)
            latest = await self._items.get_system(org_id, item_id)
            if latest is not None and latest.revision == item.revision:
                return True
        return False

    async def index_or_enqueue(self, item: Item) -> bool:
        """Index now; on any HiveError queue a durable job. Returns True if indexed inline."""
        try:
            return await self.index_current(item.org_id, item.id)
        except HiveError:
            await self._jobs.enqueue(
                item.org_id, INDEX_JOB, {"item_id": item.id}, dedupe_key=f"index:{item.id}"
            )
            return False

    async def reindex_org(self, org_id: str, page: int = 500) -> int:
        """Queue an index job for every item in the org (e.g. after switching embedder).
        Safe to repeat: jobs are deduplicated per item. Returns the number of items seen."""
        total, after = 0, None
        while ids := await self._items.list_ids_system(org_id, after, page):
            for item_id in ids:
                await self._jobs.enqueue(
                    org_id, INDEX_JOB, {"item_id": item_id}, dedupe_key=f"index:{item_id}"
                )
            total, after = total + len(ids), ids[-1]
        return total

    async def sync_acl(self, item: Item) -> None:
        await self._text.update_acl(item.id, item.acl)
        await self._vectors.update_acl(item.id, item.acl)

    async def handle_job(self, job: Job) -> None:
        """Job handler: raises on failure so the runner retries with backoff."""
        if not await self.index_current(job.org_id, job.payload["item_id"]):
            raise HiveError("item kept changing while indexing")
