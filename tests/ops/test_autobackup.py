"""One-container durability: periodic verified archives, pruning, and restore on an empty disk."""

import asyncio
import os
import sqlite3
import time

import pytest

from hive_mcp.config import Settings
from hive_mcp.ops import autobackup
from hive_mcp.ops.autobackup import archives, backup_loop, backup_once, restore_if_empty


def make_db(path, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", backup_dir=tmp_path / "share", backup_keep=3)


def touch(settings, offset=10.0):
    """Mark the database as changed after the last backup (mtimes are coarse)."""
    future = time.time() + offset
    os.utime(settings.data_dir / "hive.db", (future, future))


def test_a_backup_is_written_verified_and_skipped_until_something_changes(settings):
    make_db(settings.data_dir / "hive.db", ["a"])
    (settings.data_dir / "tokens.json").write_text("[]")
    first = backup_once(settings, None, now=time.time())
    assert first is not None and len(archives(settings.backup_dir)) == 1
    assert backup_once(settings, first, now=first + 1) == first  # unchanged: nothing written
    assert len(archives(settings.backup_dir)) == 1
    touch(settings)
    time.sleep(1.1)  # archive names carry whole seconds
    second = backup_once(settings, first, now=first + 20)
    assert second == first + 20 and len(archives(settings.backup_dir)) == 2


def test_only_the_newest_few_archives_are_kept(settings, monkeypatch):
    make_db(settings.data_dir / "hive.db", ["a"])
    stamps = iter(f"20260101-0000{n:02d}" for n in range(10))

    class Clock:
        @staticmethod
        def now(tz=None):
            from datetime import UTC, datetime

            return datetime.strptime(next(stamps), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)

    monkeypatch.setattr(autobackup, "datetime", Clock)
    for _ in range(6):
        backup_once(settings, None, now=0)
    names = [p.name for p in archives(settings.backup_dir)]
    assert names == [f"hive-auto-20260101-0000{n:02d}.tar.gz" for n in (3, 4, 5)]


def test_an_empty_disk_gets_the_newest_valid_backup_back(settings):
    make_db(settings.data_dir / "hive.db", ["old"])
    backup_once(settings, None, now=0)
    make_db(settings.data_dir / "hive.db", ["new"])
    touch(settings)
    time.sleep(1.1)  # archive names carry whole seconds
    backup_once(settings, 0, now=1)
    for f in settings.data_dir.iterdir():
        f.unlink()  # the container's disk is gone
    restored = restore_if_empty(settings)
    assert restored is not None and rows(settings.data_dir / "hive.db") == ["old", "new"]


def test_a_damaged_newest_archive_falls_back_to_the_one_before(settings):
    make_db(settings.data_dir / "hive.db", ["good"])
    backup_once(settings, None, now=0)
    time.sleep(1.1)
    touch(settings)
    backup_once(settings, 0, now=1)
    newest = archives(settings.backup_dir)[-1]
    newest.write_bytes(b"not a tar file")
    (settings.data_dir / "hive.db").unlink()
    restored = restore_if_empty(settings)
    assert restored is not None and restored != newest
    assert rows(settings.data_dir / "hive.db") == ["good"]


def test_nothing_is_restored_over_existing_data_or_without_backups_or_for_postgres(
    settings, tmp_path
):
    make_db(settings.data_dir / "hive.db", ["mine"])
    backup_once(settings, None, now=0)
    assert restore_if_empty(settings) is None and rows(settings.data_dir / "hive.db") == ["mine"]
    empty = Settings(data_dir=tmp_path / "other", backup_dir=tmp_path / "nothing-here")
    assert restore_if_empty(empty) is None
    assert restore_if_empty(Settings(data_dir=tmp_path / "x")) is None  # no backup_dir at all
    pg = Settings(
        data_dir=tmp_path / "pg", backup_dir=settings.backup_dir, profile="external",
        database_url="postgresql://x/y",
    )  # fmt: skip
    assert restore_if_empty(pg) is None


async def test_the_loop_takes_a_final_backup_when_the_server_stops(settings):
    make_db(settings.data_dir / "hive.db", ["a"])
    task = asyncio.create_task(backup_loop(settings))
    await asyncio.sleep(0.5)  # the first round runs at once
    assert len(archives(settings.backup_dir)) == 1
    make_db(settings.data_dir / "hive.db", ["b"])
    touch(settings)
    await asyncio.sleep(1.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(archives(settings.backup_dir)) == 2  # the shutdown backup caught the change


async def test_a_failing_backup_is_logged_and_retried_not_fatal(settings, caplog, monkeypatch):
    make_db(settings.data_dir / "hive.db", ["a"])
    calls = []

    def broken(settings, last, now):
        calls.append(now)
        raise OSError("share unavailable")

    monkeypatch.setattr(autobackup, "backup_once", broken)
    monkeypatch.setattr(Settings, "backup_every_minutes", 0.001, raising=False)
    task = asyncio.create_task(backup_loop(settings))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2 and "backup failed" in caplog.text
