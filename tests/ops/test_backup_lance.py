"""The LanceDB index travels with a backup, and a copy that cannot be trusted is left out loudly."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from hive_mcp.config import Settings
from hive_mcp.ops import backup_lance
from hive_mcp.ops.backup import create_backup, restore_backup, verify_backup

lancedb = pytest.importorskip("lancedb")

if TYPE_CHECKING:
    from pathlib import Path


def settings_for(data: Path) -> Settings:
    return Settings(data_dir=data, vector_store="lancedb")


def seed(data: Path, rows: int = 20) -> None:
    """A SQLite database (created by opening it) and a Lance table with `rows` rows."""
    from hive_mcp.adapters.sqlite.db import Database

    data.mkdir(parents=True, exist_ok=True)
    Database(data / "hive.db").close()
    db = lancedb.connect(str(data / "lance"))
    db.create_table(
        "vectors", data=[{"id": i, "vector": [float(i), 1.0, 0.0]} for i in range(rows)]
    )


def rows_in(data: Path) -> int:
    return lancedb.connect(str(data / "lance")).open_table("vectors").count_rows()


def test_the_lance_directory_is_archived_verified_and_restored(tmp_path):
    seed(tmp_path / "a")
    summary = create_backup(settings_for(tmp_path / "a"), tmp_path / "out.tar.gz")
    assert summary.notes == ()
    assert verify_backup(summary.path).files == summary.files
    restored = restore_backup(settings_for(tmp_path / "b"), summary.path)
    assert restored.notes == () and rows_in(tmp_path / "b") == 20


def test_restore_replaces_an_existing_lance_directory(tmp_path):
    seed(tmp_path / "a", rows=20)
    archive = create_backup(settings_for(tmp_path / "a"), tmp_path / "out.tar.gz").path
    seed(tmp_path / "b", rows=3)  # an older, smaller index
    restore_backup(settings_for(tmp_path / "b"), archive, force=True)
    assert rows_in(tmp_path / "b") == 20
    assert not (tmp_path / "b" / "lance.restoring").exists()


def test_a_copy_that_fails_once_is_repeated(tmp_path, monkeypatch):
    seed(tmp_path / "a")
    real, calls = backup_lance._copy_once, []

    def flaky(source, dest):
        calls.append(1)
        if len(calls) == 1:
            real(source, dest)
            raise FileNotFoundError("a compaction removed a file")
        real(source, dest)

    monkeypatch.setattr(backup_lance, "_copy_once", flaky)
    summary = create_backup(settings_for(tmp_path / "a"), tmp_path / "out.tar.gz")
    assert len(calls) == 2 and summary.notes == ()
    restore_backup(settings_for(tmp_path / "b"), summary.path)
    assert rows_in(tmp_path / "b") == 20


def test_without_a_consistent_copy_the_backup_goes_ahead_and_says_what_to_do(tmp_path, monkeypatch):
    seed(tmp_path / "a")

    def always_vanishing(source, dest):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(backup_lance, "_copy_once", always_vanishing)
    summary = create_backup(settings_for(tmp_path / "a"), tmp_path / "out.tar.gz")
    assert len(summary.notes) == 1 and "hive reindex" in summary.notes[0]
    assert verify_backup(summary.path).files == summary.files  # still a valid archive
    restore_backup(settings_for(tmp_path / "b"), summary.path)
    assert not (tmp_path / "b" / "lance").exists()


def test_a_copy_that_cannot_be_opened_is_not_trusted(tmp_path, monkeypatch):
    seed(tmp_path / "a")
    monkeypatch.setattr(backup_lance, "_opens", lambda directory: False)
    summary = create_backup(settings_for(tmp_path / "a"), tmp_path / "out.tar.gz")
    assert "hive reindex" in summary.notes[0]
    assert not any(n.startswith("lance/") for n in _names(summary.path))


def test_manifests_are_copied_after_the_data_they_point_to(tmp_path):
    seed(tmp_path / "a")
    shutil.rmtree(tmp_path / "copy", ignore_errors=True)
    order = []
    real_copy = shutil.copy2

    def spy(src, dst, **kw):
        order.append(str(src))
        return real_copy(src, dst, **kw)

    from unittest import mock

    with mock.patch.object(shutil, "copy2", spy):
        assert backup_lance.snapshot(tmp_path / "a" / "lance", tmp_path / "copy")
    versions = [i for i, p in enumerate(order) if "/_versions/" in p]
    others = [i for i, p in enumerate(order) if "/_versions/" not in p]
    assert versions and others and min(versions) > max(others)


def test_an_archive_without_an_index_warns_when_one_exists_here(tmp_path):
    seed(tmp_path / "a")
    shutil.rmtree(tmp_path / "a" / "lance")  # a backup taken before the Lance store was used
    archive = create_backup(Settings(data_dir=tmp_path / "a"), tmp_path / "out.tar.gz").path
    seed(tmp_path / "b")  # but this machine has one
    restored = restore_backup(settings_for(tmp_path / "b"), archive, force=True)
    assert len(restored.notes) == 1 and "hive reindex" in restored.notes[0]


def _names(archive: Path) -> list[str]:
    import tarfile

    with tarfile.open(archive) as tar:
        return tar.getnames()
