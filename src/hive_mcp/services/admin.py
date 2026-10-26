"""Read-only administration: who stores how much, and what has happened (the audit trail)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.domain.errors import PermissionDenied, ValidationError
from hive_mcp.domain.principal import Role

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.audit import AuditLog, AuditRecord
    from hive_mcp.ports.maintenance import AuthorUsage, ItemMaintenance

MAX_AUDIT_PAGE = 500


class AdminService:
    def __init__(self, maintenance: ItemMaintenance, audit: AuditLog) -> None:
        self._maintenance, self._audit = maintenance, audit

    @staticmethod
    def _require_admin(principal: Principal) -> None:
        if principal.role < Role.ADMIN:
            raise PermissionDenied("this needs an admin")

    async def usage(self, admin: Principal) -> list[AuthorUsage]:
        self._require_admin(admin)
        return await self._maintenance.usage(admin.org_id)

    async def audit(  # noqa: PLR0913 - the filters
        self,
        admin: Principal,
        *,
        actor: str | None = None,
        action: str | None = None,
        target: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[AuditRecord]:
        self._require_admin(admin)
        if not 1 <= limit <= MAX_AUDIT_PAGE or after_seq < 0:
            raise ValidationError(f"limit must be 1-{MAX_AUDIT_PAGE} and after_seq >= 0")
        return await self._audit.list(
            admin.org_id,
            after_seq=after_seq,
            limit=limit,
            actor_id=actor or None,
            action=action or None,
            target_id=target or None,
        )
