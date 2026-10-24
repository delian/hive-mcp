"""In-memory AuditLog."""

from __future__ import annotations

from hive_mcp.ports.audit import AuditEvent, AuditRecord


class MemoryAudit:
    def __init__(self) -> None:
        self._rows: list[AuditRecord] = []

    async def append(self, event: AuditEvent) -> int:
        seq = len(self._rows) + 1
        self._rows.append(AuditRecord(seq, event))
        return seq

    async def list(self, org_id: str, *, after_seq: int = 0, limit: int = 100) -> list[AuditRecord]:
        rows = [r for r in self._rows if r.event.org_id == org_id and r.seq > after_seq]
        return rows[:limit]
