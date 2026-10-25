"""The embedded-profile backend: one directory holds the SQLite file and the blobs."""

from __future__ import annotations

from pathlib import Path

from hive_mcp.adapters.fs.blobs import FsBlobs
from hive_mcp.adapters.sqlite.audit import SqliteAudit
from hive_mcp.adapters.sqlite.db import BUSY_SECONDS, Database
from hive_mcp.adapters.sqlite.items import SqliteItems
from hive_mcp.adapters.sqlite.jobs import SqliteJobs
from hive_mcp.adapters.sqlite.kv import SqliteKv
from hive_mcp.adapters.sqlite.promotions import SqlitePromotions
from hive_mcp.adapters.sqlite.text import SqliteText
from hive_mcp.adapters.sqlite.vectors import SqliteVectors
from hive_mcp.domain.clock import Clock, FixedClock


class SqliteBackend:
    def __init__(
        self, root: Path | str, clock: Clock | None = None, busy_seconds: float = BUSY_SECONDS
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock: Clock = clock or FixedClock()
        self.db = Database(self.root / "hive.db", busy_seconds)
        self.items = SqliteItems(self.db, self.clock)
        self.text = SqliteText(self.db)
        self.vectors = SqliteVectors(self.db)
        self.jobs = SqliteJobs(self.db, self.clock)
        self.audit = SqliteAudit(self.db)
        self.kv = SqliteKv(self.db, self.clock)
        self.promotions = SqlitePromotions(self.db)
        self.blobs = FsBlobs(self.root / "blobs")

    def close(self) -> None:
        self.db.close()
