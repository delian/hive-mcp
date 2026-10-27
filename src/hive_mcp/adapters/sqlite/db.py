"""One serialised SQLite connection, run off the event loop.

Every operation is a transaction; "database is locked" becomes a retryable HiveError instead of a crash.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
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
        self._enable_wal(busy_seconds)
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        migrate(self._conn)

    def _enable_wal(self, busy_seconds: float) -> None:
        """Switching the journal mode takes a lock that SQLite's busy handler does not wait for,
        so processes opening a fresh database together would fail with "database is locked"."""
        deadline = time.monotonic() + busy_seconds
        while True:
            try:
                if str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    self._conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise StorageBusy("storage busy, retry") from exc
                time.sleep(0.01)

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
