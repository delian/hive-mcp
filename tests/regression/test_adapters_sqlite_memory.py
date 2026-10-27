"""Regression tests for the SQLite adapter (see the reports for the BUG-ADP numbering)."""

from __future__ import annotations

import threading

from hive_mcp.adapters.sqlite.db import Database


def test_concurrent_first_open_of_one_database_file(tmp_path):
    """BUG-ADP-2: several processes (here threads, each with its own connection) opening a fresh
    database at once must all succeed: the migration version was read outside the write lock (so
    the loser re-ran migration 1: "table items already exists") and switching to WAL raised
    "database is locked"."""
    for trial in range(8):
        path = tmp_path / f"t{trial}.db"
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def open_it(path=path, errors=errors, barrier=barrier) -> None:
            barrier.wait()
            try:
                Database(path).close()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
