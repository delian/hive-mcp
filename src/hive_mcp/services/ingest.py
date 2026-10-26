"""Turn files into knowledge: extract text, split long documents, store as linked DOC items."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.domain.item import MAX_TITLE, Item, Kind
from hive_mcp.domain.tags import normalize_tags
from hive_mcp.ingest.extract import extract_text
from hive_mcp.ingest.fetch import fetch
from hive_mcp.ingest.sections import MAX_SECTION_CHARS, split_sections
from hive_mcp.services.models import RememberRequest

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope
    from hive_mcp.ingest.fetch import FetchPolicy
    from hive_mcp.ingest.ocr import OcrConfig
    from hive_mcp.services.entities import EntityService
    from hive_mcp.services.graph import GraphService
    from hive_mcp.services.knowledge import KnowledgeService

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_SECTIONS = 200
log = logging.getLogger(__name__)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class IngestResult:
    name: str
    items: list[Item]
    created: int  # sections newly stored; the rest were already there
    redactions: dict[str, int] = field(default_factory=dict)


def file_slug(name: str) -> str:
    return _NON_SLUG.sub("-", name.lower()).strip("-")[:60] or "file"


class IngestService:
    def __init__(  # noqa: PLR0913 - collaborators and options
        self,
        knowledge: KnowledgeService,
        graph: GraphService,
        *,
        section_chars: int = MAX_SECTION_CHARS,
        entities: EntityService | None = None,
        url_policy: FetchPolicy | None = None,
        fetch_hooks: dict[str, Any] | None = None,
        ocr: OcrConfig | None = None,
    ) -> None:
        self._ocr = ocr
        self._url_policy, self._fetch_hooks = url_policy, fetch_hooks or {}
        self._knowledge, self._graph, self._section_chars = knowledge, graph, section_chars
        self._entities = entities

    async def ingest(  # noqa: PLR0913 - keyword-only options
        self,
        principal: Principal,
        name: str,
        data: bytes,
        *,
        tags: tuple[str, ...] = (),
        scope: Scope | None = None,
        kind: Kind = Kind.DOC,
        link_entities: bool = True,
        source: str | None = None,
    ) -> IngestResult:
        """Idempotent: ingesting the same bytes with the same name, tags and scope again stores
        nothing new. Parts are linked: each `part_of` the first, and chained with `next`."""
        if not name.strip() or "\x00" in name:
            raise ValidationError("file name is required")
        if len(data) > MAX_FILE_BYTES:
            raise ValidationError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        # Parsing untrusted PDFs/Office files is CPU-bound: keep it off the event loop.
        extracted = await asyncio.to_thread(extract_text, name, data, self._ocr)
        sections = await asyncio.to_thread(split_sections, extracted.text, self._section_chars)
        if len(sections) > MAX_SECTIONS:
            raise ValidationError(f"document too long ({len(sections)} parts, max {MAX_SECTIONS})")
        base = (extracted.title or name).strip()[: MAX_TITLE - 24]
        file_tags = (*tags, f"file/{file_slug(name)}", f"format/{extracted.format}")
        fingerprint = hashlib.sha256(
            "\0".join([name, str(scope), *sorted(normalize_tags(file_tags)), str(kind)]).encode()
            + b"\0"
            + data
        ).hexdigest()
        items: list[Item] = []
        created = 0
        redactions: dict[str, int] = {}
        for index, section in enumerate(sections, start=1):
            label = f"{base} ({index}/{len(sections)})" if len(sections) > 1 else base
            title = f"{label} - {section.heading}" if section.heading else label
            result = await self._knowledge.remember(
                principal,
                RememberRequest(
                    title=title[:MAX_TITLE],
                    body=section.text,
                    kind=kind,
                    tags=file_tags,
                    scope=scope,
                    idempotency_key=f"ingest:{fingerprint}:{index}",
                    source=source or f"file:{name[:200]}",
                ),
            )
            items.append(result.item)
            created += result.created
            for k, v in result.redactions.items():
                redactions[k] = redactions.get(k, 0) + v
        await self._link_parts(principal, items)
        if self._entities and link_entities:
            await self._link_entities(principal, items, [s.text for s in sections])
        return IngestResult(name, items, created, redactions)

    async def ingest_url(
        self,
        principal: Principal,
        url: str,
        *,
        tags: tuple[str, ...] = (),
        scope: Scope | None = None,
    ) -> IngestResult:
        """Fetch a web page or document and ingest it (only if the operator enabled this)."""
        if self._url_policy is None:
            raise ValidationError("fetching URLs is not enabled on this server")
        fetched = await fetch(url, self._url_policy, **self._fetch_hooks)
        return await self.ingest(
            principal,
            fetched.name,
            fetched.data,
            tags=tags,
            scope=scope,
            source=f"url:{fetched.url}"[:400],
        )

    async def _link_entities(
        self, principal: Principal, items: list[Item], texts: list[str]
    ) -> None:
        """Point each part at the entities (names/aliases you defined) that it mentions."""
        assert self._entities  # noqa: S101 - guarded by the caller
        for item, text in zip(items, texts, strict=True):
            try:
                for entity_id in await self._entities.detect_mentions(principal, text):
                    await self._graph.link(principal, item.id, entity_id, "mentions")
            except HiveError:
                log.warning("entity linking skipped for %s", item.id)

    async def _link_parts(self, principal: Principal, items: list[Item]) -> None:
        head = items[0]
        for previous, part in itertools.pairwise(items):
            try:
                await self._graph.link(principal, part.id, head.id, "part_of")
                await self._graph.link(principal, previous.id, part.id, "next")
            except HiveError:  # links are best effort: the content is already safely stored
                break


__all__ = ["IngestResult", "IngestService"]
