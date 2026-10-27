"""Keeps the knowledge base equal to a connector's documents: add, update, retire.

State per connector (which source document became which items, and at what version) lives in the
key-value store, so every replica sees it and a restart loses nothing. A changed document is
ingested again and its previous items are archived (not deleted: history stays); a document that is
gone from the source is archived too, but only when the whole listing succeeded and not suspiciously
much disappeared at once (an empty or broken listing must not wipe the knowledge base).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from hive_mcp.connectors.base import SyncReport
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.item import ItemPatch, Status
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ports.audit import AuditEvent

if TYPE_CHECKING:
    from hive_mcp.connectors.base import Connector, Document
    from hive_mcp.connectors.spec import ConnectorSpec
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.services.ingest import IngestService
    from hive_mcp.services.knowledge import KnowledgeService

log = logging.getLogger(__name__)
STATE_NS = "connector:state"
STATUS_NS = "connector:status"
MASS_RETIRE_FRACTION = 0.5
MASS_RETIRE_MIN = 5
SEPARATOR = b"\0"  # pragma: no mutate - any fixed separator hashes consistently


def hkey(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class ConnectorSync:
    def __init__(
        self,
        ingest: IngestService,
        knowledge: KnowledgeService,
        kv: KeyValueStore,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._ingest, self._knowledge, self._kv = ingest, knowledge, kv
        self._audit, self._clock = audit, clock

    async def run(
        self, spec: ConnectorSpec, connector: Connector, *, allow_mass_retire: bool = False
    ) -> SyncReport:
        who = Principal(spec.owner, spec.org, Role.MEMBER, frozenset(), frozenset({spec.project}))
        scope = Scope(ScopeLevel.PROJECT, spec.project)
        stored = await self._kv.get(STATE_NS, spec.name)
        state: dict[str, dict[str, Any]] = dict((stored or {}).get("docs", {}))
        report, seen = SyncReport(), set()
        try:
            async for doc in connector.documents():
                seen.add(hkey(doc.key))
                await self._one(who, scope, spec, doc, state, report)
        except HiveError as exc:
            report.aborted = f"listing failed ({exc}); nothing retired"
        except Exception as exc:
            log.exception("connector %s crashed", spec.name)
            report.aborted = f"{exc.__class__.__name__}; nothing retired"
        if report.aborted is None and not report.failed:
            await self._retire_missing(who, state, seen, report, allow_mass_retire)
        await self._kv.put(STATE_NS, spec.name, {"docs": state})
        await self._record(spec, report)
        return report

    async def _one(  # noqa: PLR0913, PLR0917 - the pieces of one document's sync
        self, who: Principal, scope: Scope, spec: ConnectorSpec, doc: Document,
        state: dict[str, dict[str, Any]], report: SyncReport,
    ) -> None:  # fmt: skip
        key, previous = hkey(doc.key), state.get(hkey(doc.key))
        if previous and doc.version and previous.get("ver") == doc.version:
            report.unchanged += 1
            return
        try:
            name, data = await doc.load()
            fingerprint = hashlib.sha256(name.encode() + SEPARATOR + data).hexdigest()
            if previous and previous.get("fp") == fingerprint:
                state[key] = {**previous, "ver": doc.version}
                report.unchanged += 1
                return
            tags = (*spec.tags, *doc.tags, f"connector/{spec.name}")
            source = f"connector:{spec.name}:{doc.key}"[:400]
            done = await self._ingest.ingest(who, name, data, scope=scope, tags=tags, source=source)
        except HiveError as exc:
            report.failed[doc.label] = str(exc)[:200]
            return
        ids = [i.id for i in done.items]
        if previous:
            await self._archive(who, [i for i in previous["items"] if i not in ids])
            report.updated += 1
        else:
            report.created += 1
        state[key] = {"key": doc.key, "ver": doc.version, "fp": fingerprint, "items": ids}

    async def _retire_missing(
        self, who: Principal, state: dict[str, dict[str, Any]], seen: set[str],
        report: SyncReport, allow_mass_retire: bool,
    ) -> None:  # fmt: skip
        gone = [k for k in state if k not in seen]
        if not gone:
            return
        if (
            not allow_mass_retire
            and len(state) >= MASS_RETIRE_MIN
            and len(gone) > len(state) * MASS_RETIRE_FRACTION
        ):
            report.aborted = (
                f"{len(gone)} of {len(state)} documents vanished at once: nothing retired "
                "(run `hive connectors run --force` if that is right)"
            )
            return
        for k in gone:
            await self._archive(who, state.pop(k)["items"])
            report.retired += 1

    async def _archive(self, who: Principal, item_ids: list[str]) -> None:
        for item_id in item_ids:
            try:
                view = await self._knowledge.get(who, item_id)
                if view.item.status is not Status.ARCHIVED:
                    patch = ItemPatch(status=Status.ARCHIVED)
                    await self._knowledge.update(who, item_id, patch, view.item.revision)
            except HiveError:
                log.warning("could not archive %s", item_id)  # already gone or edited by a person

    async def _record(self, spec: ConnectorSpec, report: SyncReport) -> None:
        now = self._clock.now()
        await self._kv.put(
            STATUS_NS,
            spec.name,
            {
                "at": now.isoformat(),
                "summary": report.line(),
                "failed": dict(list(report.failed.items())[:20]),
            },
        )
        details = {"connector": spec.name, "result": report.line()}
        await self._audit.append(
            AuditEvent(spec.org, f"connector:{spec.name}", "connector.sync", now, None, details)
        )
