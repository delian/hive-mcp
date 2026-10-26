"""Append-only audit trail for writes, retags, promotions, exports and deletes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 - runtime-evaluated dataclass field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuditEvent:
    org_id: str
    actor_id: str
    action: str
    at: datetime
    target_id: str | None = None
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int  # strictly increasing per store; use as the pagination cursor
    event: AuditEvent


class AuditLog(Protocol):
    async def append(self, event: AuditEvent) -> int: ...

    async def list(  # noqa: PLR0913 - keyword-only filters
        self,
        org_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        actor_id: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
    ) -> list[AuditRecord]:
        """Oldest first, only this org's events, seq > after_seq, optionally filtered."""
