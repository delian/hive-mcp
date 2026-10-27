"""SQLite AuditLog (events are never edited or deleted; only a person's identity can be erased)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hive_mcp.adapters.sqlite.codec import parse_ts, ts
from hive_mcp.ports.audit import AuditEvent, AuditRecord

if TYPE_CHECKING:
    import sqlite3

    from hive_mcp.adapters.sqlite.db import Database


MAX_SEQ = 2**62


class SqliteAudit:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, event: AuditEvent) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO audit (org_id, actor_id, action, target_id, at, details) VALUES (?,?,?,?,?,?)",
                (
                    event.org_id,
                    event.actor_id,
                    event.action,
                    event.target_id,
                    ts(event.at),
                    json.dumps(event.details, sort_keys=True),
                ),
            )
            return int(cur.lastrowid or 0)

        return await self._db.run(tx, write=True)

    async def pseudonymise(self, org_id: str, person_id: str, alias: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            changed = conn.execute(
                "UPDATE audit SET actor_id = ? WHERE org_id = ? AND actor_id = ?",
                (alias, org_id, person_id),
            ).rowcount
            changed += conn.execute(
                "UPDATE audit SET target_id = ? WHERE org_id = ? AND target_id = ?",
                (alias, org_id, person_id),
            ).rowcount
            return changed

        return await self._db.run(tx, write=True)

    async def list(  # noqa: PLR0913
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
        order = "DESC" if newest_first or before_seq is not None else "ASC"

        def tx(conn: sqlite3.Connection) -> list[AuditRecord]:
            rows = conn.execute(
                f"SELECT * FROM audit WHERE org_id = ? AND seq > ? AND seq < ? "  # noqa: S608
                "AND (? IS NULL OR actor_id = ?) AND (? IS NULL OR action = ?) "
                f"AND (? IS NULL OR target_id = ?) ORDER BY seq {order} LIMIT ?",
                (
                    org_id,
                    after_seq,
                    MAX_SEQ if before_seq is None else before_seq,
                    actor_id,
                    actor_id,
                    action,
                    action,
                    target_id,
                    target_id,
                    limit,
                ),
            ).fetchall()
            return [
                AuditRecord(
                    r["seq"],
                    AuditEvent(
                        r["org_id"],
                        r["actor_id"],
                        r["action"],
                        parse_ts(r["at"]),
                        r["target_id"],
                        json.loads(r["details"]),
                    ),
                )
                for r in rows
            ]

        return await self._db.run(tx)
