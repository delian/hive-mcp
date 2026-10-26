"""PostgreSQL AuditLog (append-only by construction: no update or delete is exposed)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

from hive_mcp.ports.audit import AuditEvent, AuditRecord

if TYPE_CHECKING:
    from hive_mcp.adapters.postgres.db import PgDatabase


class PgAudit:
    def __init__(self, db: PgDatabase) -> None:
        self._db = db

    async def append(self, event: AuditEvent) -> int:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "INSERT INTO audit (org_id, actor_id, action, target_id, at, details) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING seq",
                (
                    event.org_id,
                    event.actor_id,
                    event.action,
                    event.target_id,
                    event.at,
                    Jsonb(event.details),
                ),
            )
            row = await cur.fetchone()
            return int(row["seq"]) if row else 0

    async def list(  # noqa: PLR0913
        self,
        org_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        actor_id: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
    ) -> list[AuditRecord]:
        async with self._db.tx() as conn:
            cur = await conn.execute(
                "SELECT * FROM audit WHERE org_id = %s AND seq > %s "
                "AND (%s::text IS NULL OR actor_id = %s) AND (%s::text IS NULL OR action = %s) "
                "AND (%s::text IS NULL OR target_id = %s) ORDER BY seq LIMIT %s",
                (
                    org_id,
                    after_seq,
                    actor_id,
                    actor_id,
                    action,
                    action,
                    target_id,
                    target_id,
                    limit,
                ),
            )
            return [
                AuditRecord(
                    r["seq"],
                    AuditEvent(
                        r["org_id"],
                        r["actor_id"],
                        r["action"],
                        r["at"],
                        r["target_id"],
                        dict(r["details"]),
                    ),
                )
                for r in await cur.fetchall()
            ]
