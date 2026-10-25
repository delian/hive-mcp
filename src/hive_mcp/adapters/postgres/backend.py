"""The external-profile backend: Postgres for everything relational, filesystem (for now) for blobs."""

from __future__ import annotations

from pathlib import Path

from hive_mcp.adapters.fs.blobs import FsBlobs
from hive_mcp.adapters.postgres.audit import PgAudit
from hive_mcp.adapters.postgres.db import PgDatabase
from hive_mcp.adapters.postgres.items import PgItems
from hive_mcp.adapters.postgres.jobs import PgJobs
from hive_mcp.adapters.postgres.kv import PgKv
from hive_mcp.adapters.postgres.promotions import PgPromotions
from hive_mcp.adapters.postgres.text import PgText
from hive_mcp.adapters.postgres.vectors import PgVectors
from hive_mcp.domain.clock import Clock, FixedClock


class PostgresBackend:
    def __init__(self, db: PgDatabase, blobs_root: Path | str, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or FixedClock()
        self.db = db
        self.items = PgItems(db, self.clock)
        self.text = PgText(db)
        self.vectors = PgVectors(db)
        self.jobs = PgJobs(db, self.clock)
        self.audit = PgAudit(db)
        self.kv = PgKv(db, self.clock)
        self.promotions = PgPromotions(db)
        self.blobs = FsBlobs(Path(blobs_root))

    @classmethod
    async def connect(
        cls,
        dsn: str,
        blobs_root: Path | str,
        *,
        clock: Clock | None = None,
        schema: str | None = None,
        max_pool: int = 10,
    ) -> PostgresBackend:
        db = await PgDatabase.open(dsn, schema=schema, max_size=max_pool)
        return cls(db, blobs_root, clock)

    async def close(self) -> None:
        await self.db.close()
