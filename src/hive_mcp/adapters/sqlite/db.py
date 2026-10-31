"""SQLite access off the event loop: one serialised writer plus a small pool of readers.

Every operation is a transaction; "database is locked" becomes a retryable HiveError instead of a
crash. WAL mode lets readers run beside the writer, so a search never waits behind a write or
behind another search; the readers are opened read-only, so a "read" that tries to write fails
loudly instead of silently taking the writer's place.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
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
READERS = 4  # read-only connections; 0 sends reads through the writer as before


__all__ = ["BUSY_SECONDS", "Database", "StorageBusy"]


class Database:
    def __init__(
        self, path: Path | str, busy_seconds: float = BUSY_SECONDS, readers: int = READERS
    ) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=busy_seconds
        )
        self._conn.row_factory = sqlite3.Row
        self._enable_wal(busy_seconds)
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        migrate(self._conn)
        self._readers: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self._reader_count = readers
        for _ in range(readers):  # after the migration, so they see the final schema
            reader = sqlite3.connect(
                str(path), isolation_level=None, check_same_thread=False, timeout=busy_seconds
            )
            reader.row_factory = sqlite3.Row
            reader.execute("PRAGMA query_only = ON")
            self._readers.put(reader)

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
        if not write and self._reader_count:
            return self._read(fn)
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

    def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._readers.get()  # waits when every reader is busy
        try:
            try:
                conn.execute("BEGIN")
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    raise StorageBusy("storage busy, retry") from exc
                raise
            return result
        finally:
            self._readers.put(conn)

    async def run(self, fn: Callable[[sqlite3.Connection], T], *, write: bool = False) -> T:
        return await asyncio.to_thread(self.call, fn, write=write)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        for _ in range(self._reader_count):
            with contextlib.suppress(queue.Empty, sqlite3.Error):
                self._readers.get(timeout=1).close()
