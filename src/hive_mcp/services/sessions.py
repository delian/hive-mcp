"""Session and compaction reports: a short searchable summary plus the full log kept as a file."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import NotFound, ValidationError
from hive_mcp.domain.item import Kind
from hive_mcp.ports.blobs import BlobRef
from hive_mcp.security.redact import redact
from hive_mcp.services.models import RememberRequest

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.blobs import BlobStore
    from hive_mcp.services.knowledge import KnowledgeService
    from hive_mcp.services.models import RememberResult

MAX_LOG_BYTES = 5 * 1024 * 1024
BLOB_PREFIX = "blob:"


@dataclass(frozen=True, slots=True)
class SessionReport:
    summary: str
    log: str | None = None
    outcomes: tuple[str, ...] = ()
    project: str | None = None  # recorded as a tag; the report itself stays private
    tags: tuple[str, ...] = ()
    idempotency_key: str | None = None


class SessionService:
    def __init__(self, knowledge: KnowledgeService, blobs: BlobStore) -> None:
        self._knowledge, self._blobs = knowledge, blobs

    async def _report(
        self, principal: Principal, kind: Kind, label: str, report: SessionReport
    ) -> RememberResult:
        first_line = next((ln.strip() for ln in report.summary.splitlines() if ln.strip()), label)
        body = report.summary.strip()
        if report.outcomes:
            body += "\n\nOutcomes:\n" + "\n".join(f"- {o}" for o in report.outcomes)
        source, log_findings = None, {}
        if report.log is not None:
            clean = redact(report.log)  # logs are where secrets leak: mask before storing
            data = clean.text.encode()
            if len(data) > MAX_LOG_BYTES:
                raise ValidationError(f"log larger than {MAX_LOG_BYTES} bytes")
            ref = await self._blobs.put(principal.org_id, data)
            source, log_findings = f"{BLOB_PREFIX}{ref.sha256}:{ref.size}", clean.findings
        tags = (
            *report.tags,
            f"kind/{kind.value}",
            *([f"project/{report.project}"] if report.project else []),
        )
        result = await self._knowledge.remember(
            principal,
            RememberRequest(
                title=f"{label}: {first_line[:120]}",
                body=body,
                kind=kind,
                tags=tags,
                source=source,
                idempotency_key=report.idempotency_key,
            ),
        )
        merged = {
            k: result.redactions.get(k, 0) + log_findings.get(k, 0)
            for k in {*result.redactions, *log_findings}
        }
        return type(result)(result.item, result.created, result.indexed, merged)

    async def report_session(self, principal: Principal, report: SessionReport) -> RememberResult:
        return await self._report(principal, Kind.SESSION, "Session", report)

    async def report_compaction(
        self, principal: Principal, report: SessionReport
    ) -> RememberResult:
        return await self._report(principal, Kind.COMPACTION, "Compaction", report)

    async def get_log(self, principal: Principal, item_id: str) -> str:
        """The stored log of a report the caller can see (access is checked through the item)."""
        view = await self._knowledge.get(principal, item_id)
        source = view.item.source
        if not source or not source.startswith(BLOB_PREFIX):
            raise NotFound(f"{item_id} has no stored log")
        sha, _, size = source[len(BLOB_PREFIX) :].partition(":")
        return (await self._blobs.get(BlobRef(view.item.org_id, sha, int(size or 0)))).decode()
