import os
import shutil
import tarfile

import pytest
from typer.testing import CliRunner

from hive_mcp.cli import cli

runner = CliRunner()


def seed(tmp_path, monkeypatch, name="data"):
    data = tmp_path / name
    monkeypatch.setenv("HIVE_DATA_DIR", str(data))
    doc = tmp_path / "doc.md"
    doc.write_text("# Pulse\nradar pulse timing notes")
    assert runner.invoke(cli, ["ingest", str(doc), "--org", "o1", "--user", "u1"]).exit_code == 0
    assert (
        runner.invoke(
            cli, ["token", "create", "--label", "t", "--subject", "u1", "--org", "o1"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli, ["user", "add", "a@x.io", "--org", "o1", "--approves", "team:t1"]
        ).exit_code
        == 0
    )
    (data / "secret.key").write_text("cookie-secret")
    blob = data / "blobs" / "ab"
    blob.mkdir(parents=True)
    (blob / "abcdef").write_text("a stored log")
    return data


def vectors_line():
    return runner.invoke(cli, ["vectors", "status"]).output


def test_backup_then_restore_into_an_empty_directory_brings_everything_back(tmp_path, monkeypatch):
    data = seed(tmp_path, monkeypatch)
    out = runner.invoke(cli, ["backup", str(tmp_path)])
    assert out.exit_code == 0 and "verified" in out.output
    (archive,) = tmp_path.glob("hive-backup-*.tar.gz")
    assert runner.invoke(cli, ["backup-verify", str(archive)]).exit_code == 0
    shutil.rmtree(data)
    assert "0/0" in vectors_line()  # fresh and empty
    restored = runner.invoke(
        cli, ["restore", str(archive), "--force"]
    )  # status created an empty db
    assert restored.exit_code == 0, restored.output
    assert "1/1 items" in vectors_line()  # the ingested document is back and indexed
    assert (data / "secret.key").read_text() == "cookie-secret"
    assert (data / "blobs" / "ab" / "abcdef").read_text() == "a stored log"
    assert "t" in runner.invoke(cli, ["token", "list"]).output
    assert "team:t1" in runner.invoke(cli, ["user", "list"]).output
    assert oct((data / "secret.key").stat().st_mode & 0o777) == "0o600"


def test_restore_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    data = seed(tmp_path, monkeypatch)
    archive = tmp_path / "b.tar.gz"
    assert runner.invoke(cli, ["backup", str(archive)]).exit_code == 0
    refused = runner.invoke(cli, ["restore", str(archive)])
    assert refused.exit_code == 1 and "--force" in refused.output
    assert runner.invoke(cli, ["restore", str(archive), "--force"]).exit_code == 0
    assert (data / "hive.db").exists()


def test_backup_is_a_consistent_snapshot_even_with_a_live_wal(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    from hive_mcp.adapters.sqlite.backend import SqliteBackend

    live = SqliteBackend(tmp_path / "data")  # a server holding the database open (WAL mode)
    try:
        assert runner.invoke(cli, ["backup", str(tmp_path / "live.tar.gz")]).exit_code == 0
    finally:
        live.close()


def test_corrupt_or_foreign_archives_are_refused(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    archive = tmp_path / "b.tar.gz"
    runner.invoke(cli, ["backup", str(archive)])
    # 1. a member altered after the fact
    work = tmp_path / "x"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    (work / "state" / "secret").write_text("tampered")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(work).as_posix())
    out = runner.invoke(cli, ["backup-verify", str(bad)])
    assert out.exit_code == 1 and "checksum" in out.output
    # 2. not a backup at all
    junk = tmp_path / "junk.tar.gz"
    with tarfile.open(junk, "w:gz") as tar:
        tar.add(work / "state" / "secret", arcname="hello.txt")
    assert runner.invoke(cli, ["backup-verify", str(junk)]).exit_code == 1
    # 3. path traversal in member names
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 0
        import io

        tar.addfile(info, io.BytesIO(b""))
    out = runner.invoke(cli, ["restore", str(evil), "--force"])
    assert out.exit_code == 1 and "unsafe" in out.output and not (tmp_path / "escape.txt").exists()
    # 4. an extra file that the manifest does not list
    extra = tmp_path / "extra.tar.gz"
    (work / "blobs").mkdir(exist_ok=True)
    (work / "blobs" / "sneaky").write_text("x")
    with tarfile.open(extra, "w:gz") as tar:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(work).as_posix())
    assert "manifest" in runner.invoke(cli, ["backup-verify", str(extra)]).output


def test_profile_mismatch_is_refused(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    archive = tmp_path / "b.tar.gz"
    runner.invoke(cli, ["backup", str(archive)])
    monkeypatch.setenv("HIVE_PROFILE", "external")
    monkeypatch.setenv("HIVE_DATABASE_URL", "postgresql://nobody@localhost/none")
    out = runner.invoke(cli, ["restore", str(archive), "--force"])
    assert out.exit_code == 1 and "embedded profile" in out.output


PG = os.environ.get("HIVE_TEST_PG_DSN")


@pytest.mark.skipif(
    not (PG and shutil.which("pg_dump") and shutil.which("pg_restore")),
    reason="needs Postgres + client tools",
)
def test_postgres_backup_and_restore_roundtrip(tmp_path, monkeypatch):
    import psycopg

    admin = psycopg.connect(PG, autocommit=True)
    admin.execute("DROP DATABASE IF EXISTS hive_bk")
    admin.execute("CREATE DATABASE hive_bk")
    dsn = PG.replace("dbname=hive_test", "dbname=hive_bk")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    monkeypatch.setenv("HIVE_PROFILE", "external")
    monkeypatch.setenv("HIVE_DATABASE_URL", dsn)
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    doc = tmp_path / "d.md"
    doc.write_text("pelican notes for postgres backup")
    assert runner.invoke(cli, ["ingest", str(doc), "--org", "o1", "--user", "u1"]).exit_code == 0
    archive = tmp_path / "pg.tar.gz"
    assert runner.invoke(cli, ["backup", str(archive)]).exit_code == 0, "backup failed"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    assert "0/0" in vectors_line()
    assert runner.invoke(cli, ["restore", str(archive)]).exit_code == 1  # needs --force
    ok = runner.invoke(cli, ["restore", str(archive), "--force"])
    assert ok.exit_code == 0, ok.output
    assert "1/1 items" in vectors_line()
    admin.execute("DROP DATABASE hive_bk WITH (FORCE)")
    admin.close()


def test_a_damaged_database_inside_a_consistent_archive_is_caught(tmp_path, monkeypatch):
    import hashlib
    import json

    seed(tmp_path, monkeypatch)
    archive = tmp_path / "b.tar.gz"
    runner.invoke(cli, ["backup", str(archive)])
    work = tmp_path / "x"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    db = work / "db" / "hive.db"
    blob = bytearray(db.read_bytes())
    for i in range(4096 * 2, min(len(blob), 4096 * 6)):  # scribble over data pages, keep the header
        blob[i] = 0xFF
    db.write_bytes(bytes(blob))
    manifest = json.loads((work / "manifest.json").read_text())  # hashes made to match the damage
    manifest["files"]["db/hive.db"] = {
        "sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
        "size": db.stat().st_size,
    }
    (work / "manifest.json").write_text(json.dumps(manifest))
    bad = tmp_path / "damaged.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for p in sorted(work.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(work).as_posix())
    out = runner.invoke(cli, ["backup-verify", str(bad)])
    assert out.exit_code == 1 and (
        "damaged" in out.output or "malformed" in out.output or "error" in out.output
    )
