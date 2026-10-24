"""One serialised SQLite connection, run off the event loop.

Every operation is a transaction; "database is locked" becomes a retryable HiveError instead of a crash.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from typing import TYPE_CHECKING, TypeVar

from hive_mcp.adapters.sqlite.schema import migrate
from hive_mcp.domain.errors import StorageBusy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")
BUSY_SECONDS = 10.0


__all__ = ["BUSY_SECONDS", "Database", "StorageBusy"]


class Database:
    def __init__(self, path: Path | str, busy_seconds: float = BUSY_SECONDS) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=busy_seconds
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        migrate(self._conn)

    def call(self, fn: Callable[[sqlite3.Connection], T], *, write: bool = False) -> T:
        """Run `fn` in one transaction (blocking; use `run` from async code)."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    result = fn(self._conn)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    raise StorageBusy("storage busy, retry") from exc
                raise
            return result

    async def run(self, fn: Callable[[sqlite3.Connection], T], *, write: bool = False) -> T:
        return await asyncio.to_thread(self.call, fn, write=write)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
