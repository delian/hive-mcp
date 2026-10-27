"""Postgres inside the container: settings, the supervisor's life cycle, backups and restore."""

import os
import shutil
import time
from pathlib import Path

import psycopg
import pytest

from hive_mcp.config import Profile, Settings
from hive_mcp.config_extras import EMBEDDED_PG_DSN, EMBEDDED_PG_SOCKET
from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops import autobackup, embedded_pg
from hive_mcp.ops.autobackup import archives, backup_once, restore_if_empty
from hive_mcp.ops.embedded_pg import EmbeddedPostgres


def test_the_flag_selects_the_external_profile_with_its_own_database():
    settings = Settings(embedded_postgres=True)
    assert settings.profile is Profile.EXTERNAL and settings.database_url == EMBEDDED_PG_DSN
    assert EMBEDDED_PG_SOCKET in EMBEDDED_PG_DSN and "password" not in EMBEDDED_PG_DSN


def test_it_refuses_to_be_combined_with_another_database():
    with pytest.raises(ValueError, match="runs its own database"):
        Settings(embedded_postgres=True, database_url="postgresql://elsewhere/db")
    with pytest.raises(ValueError, match="runs its own database"):
        Settings(embedded_postgres=True, profile="embedded")
    assert Settings(embedded_postgres=True, profile="external").profile is Profile.EXTERNAL
    with pytest.raises(ValueError, match="REPLICAS must be 1"):
        Settings(embedded_postgres=True, replicas=2)


def test_without_postgres_in_the_image_the_message_says_how_to_build_it(monkeypatch):
    monkeypatch.setattr(embedded_pg.shutil, "which", lambda _name: None)
    monkeypatch.setattr(embedded_pg, "PG_ROOT", Path("/nonexistent-pg"))
    with pytest.raises(ValidationError, match="WITH_POSTGRES=true"):
        embedded_pg.bin_dir()


def test_the_newest_version_under_debians_directory_is_used(tmp_path, monkeypatch):
    for version in ("9", "16", "17"):
        (tmp_path / version / "bin").mkdir(parents=True)
        (tmp_path / version / "bin" / "pg_ctl").write_text("")
    monkeypatch.setattr(embedded_pg.shutil, "which", lambda _name: None)
    monkeypatch.setattr(embedded_pg, "PG_ROOT", tmp_path)
    assert embedded_pg.bin_dir() == tmp_path / "17" / "bin"


needs_postgres = pytest.mark.skipif(
    (shutil.which("initdb") is None and not list(Path("/usr/lib/postgresql").glob("*/bin/initdb")))
    or os.getuid() == 0,
    reason="needs PostgreSQL server binaries and a user other than root (initdb refuses root)",
)


def rows(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]


def write(dsn: str, *values: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v text)")
        for value in values:
            conn.execute("INSERT INTO t VALUES (%s)", (value,))


def wait_for_change(dsn: str, before: object) -> None:
    for _ in range(50):  # statistics are flushed a moment after the commit
        if autobackup.pg_activity(dsn) != before:
            return
        time.sleep(0.2)
    raise AssertionError("the activity counters never moved")


@needs_postgres
def test_a_cluster_is_created_survives_a_restart_and_ignores_a_stale_pid_file(tmp_path):
    pg = EmbeddedPostgres(tmp_path / "data")
    assert pg.start() is True
    write(EMBEDDED_PG_DSN, "kept")
    pg.stop()
    pg.stop()  # a second stop is harmless
    (tmp_path / "data" / "pg" / "postmaster.pid").write_text("1\n/x\n0\n0\n")  # a killed container
    again = EmbeddedPostgres(tmp_path / "data")
    try:
        assert again.start() is False  # not new this time
        assert rows(EMBEDDED_PG_DSN) == ["kept"]
    finally:
        again.stop()


@needs_postgres
def test_archives_follow_changes_and_a_replaced_container_gets_its_data_back(tmp_path):
    share = tmp_path / "share"
    first = Settings(embedded_postgres=True, data_dir=tmp_path / "a", backup_dir=share)
    pg = EmbeddedPostgres(first.data_dir)
    pg.start()
    try:
        write(EMBEDDED_PG_DSN, "one", "two")
        assert backup_once(first, None, 1.0) == 1.0
        assert len(archives(share)) == 1
        assert backup_once(first, 1.0, 2.0) == 1.0  # nothing written since: no new archive
        activity = autobackup.pg_activity(EMBEDDED_PG_DSN)
        write(EMBEDDED_PG_DSN, "three")
        wait_for_change(EMBEDDED_PG_DSN, activity)
        time.sleep(1.1)  # archive names carry the second
        assert backup_once(first, 1.0, 3.0) == 3.0
        assert len(archives(share)) == 2
    finally:
        pg.stop()
    # a brand-new container: new disk, empty cluster, the same share
    second = Settings(embedded_postgres=True, data_dir=tmp_path / "b", backup_dir=share)
    replacement = EmbeddedPostgres(second.data_dir)
    assert replacement.start() is True
    try:
        assert restore_if_empty(second, fresh_pg=False) is None  # only into a fresh cluster
        assert restore_if_empty(second, fresh_pg=True) is not None
        assert rows(EMBEDDED_PG_DSN) == ["one", "three", "two"]
    finally:
        replacement.stop()
