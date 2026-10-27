"""Audit trail for writes, retags, promotions, exports and deletes.

Append-only for content: the one permitted change is erasing a person's identity on request.
"""

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

    async def pseudonymise(self, org_id: str, person_id: str, alias: str) -> int:
        """Replace `person_id` with `alias` as actor and as target of this org's events (the
        events stay, the person goes). Returns how many events changed."""

    async def list(  # noqa: PLR0913 - keyword-only filters
        self,
        org_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        actor_id: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
        before_seq: int | None = None,
        newest_first: bool = False,
    ) -> list[AuditRecord]:
        """Only this org's events with after_seq < seq < before_seq, optionally filtered. Oldest
        first, unless `newest_first` (or `before_seq`, which pages backwards) is given."""
