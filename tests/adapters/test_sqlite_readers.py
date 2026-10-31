"""Reads do not wait behind the writer or each other; they are read-only; they see committed data."""

from __future__ import annotations

import threading
import time

import pytest

from hive_mcp.adapters.sqlite.db import Database

SETUP = "CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v INTEGER)"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "x.db")
    database.call(lambda c: c.execute(SETUP), write=True)
    yield database
    database.close()


def test_a_read_sees_what_a_write_committed_just_before(db):
    for n in range(50):
        db.call(lambda c, n=n: c.execute("INSERT INTO t VALUES (?, ?)", (f"k{n}", n)), write=True)
        count = db.call(lambda c: c.execute("SELECT count(*) FROM t").fetchone()[0])
        assert count == n + 1


def test_a_read_does_not_wait_for_a_long_write(db):
    started, release = threading.Event(), threading.Event()

    def slow_write(conn):
        conn.execute("INSERT INTO t VALUES ('slow', 1)")
        started.set()
        release.wait(5)

    writer = threading.Thread(target=lambda: db.call(slow_write, write=True))
    writer.start()
    assert started.wait(5)
    began = time.perf_counter()
    rows = db.call(lambda c: c.execute("SELECT count(*) FROM t").fetchone()[0])
    waited = time.perf_counter() - began
    release.set()
    writer.join()
    assert rows == 0 and waited < 1.0  # the uncommitted row is invisible and nothing blocked


def test_readers_run_at_the_same_time(tmp_path):
    db = Database(tmp_path / "many.db", readers=3)
    inside, gate = [], threading.Barrier(3, timeout=5)

    def read(conn):
        inside.append(1)
        gate.wait()  # all three must be inside at once for this to pass
        return conn.execute("SELECT 1").fetchone()[0]

    threads = [threading.Thread(target=lambda: db.call(read)) for _ in range(3)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(inside) == 3 and not gate.broken


def test_a_read_that_tries_to_write_is_refused_and_leaves_no_trace(db):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.call(lambda c: c.execute("INSERT INTO t VALUES ('x', 1)"))
    assert db.call(lambda c: c.execute("SELECT count(*) FROM t").fetchone()[0]) == 0


def test_without_readers_everything_goes_through_the_writer(tmp_path):
    database = Database(tmp_path / "y.db", readers=0)
    database.call(lambda c: c.execute(SETUP), write=True)
    database.call(lambda c: c.execute("INSERT INTO t VALUES ('a', 1)"))  # allowed: it is the writer
    assert database.call(lambda c: c.execute("SELECT count(*) FROM t").fetchone()[0]) == 1
    database.close()


def test_a_failed_read_returns_its_connection_to_the_pool(db):
    for _ in range(10):  # more failures than readers
        with pytest.raises(ZeroDivisionError):
            db.call(lambda c: 1 / 0)
    assert db.call(lambda c: c.execute("SELECT 1").fetchone()[0]) == 1
