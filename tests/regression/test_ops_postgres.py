"""Regression tests for the ops / Postgres bug hunt (BUG-OPS-n)."""

import asyncio
import json
import os
import threading
import time
import uuid

import psycopg
import pytest
import pytest_asyncio

from hive_mcp.ops import lease as lease_module
from hive_mcp.ops.lease import FILE, Lease

DSN = os.environ.get("HIVE_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="HIVE_TEST_PG_DSN not set")


@pytest_asyncio.fixture
async def schema():
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def test_bug_ops_1_the_lease_is_kept_fresh_while_the_holder_finishes_a_hand_off(
    tmp_path, monkeypatch
):
    """BUG-OPS-1: after answering a hand-off request the watcher thread returned, so the lease
    stopped being refreshed while the holder drained, took its final backup and closed the
    database. Once the lease looked stale (45 s) a waiting instance took it over and restored an
    older archive before the final backup was written: the newest changes were lost."""
    monkeypatch.setattr(lease_module, "WATCH_SECONDS", 0.02)
    monkeypatch.setattr(lease_module, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(lease_module, "STALE_SECONDS", 0.5)
    monkeypatch.setattr(lease_module, "POLL_SECONDS", 0.05)
    yielded = threading.Event()
    old = Lease(tmp_path, "old", revision="r1", on_handoff=yielded.set)
    old.acquire(0)
    try:
        (tmp_path / lease_module.HANDOFF_FILE).write_text(json.dumps({"revision": "r2"}))
        assert yielded.wait(2)
        time.sleep(1.2)  # the old instance is still shutting down (final backup, database close)
        # the lease must still be fresh: a second starter must not be able to take it
        with pytest.raises(Exception, match="another instance"):
            Lease(tmp_path, "new", revision="r2").acquire(0)
        assert json.loads((tmp_path / FILE).read_text())["owner"] == "old"
    finally:
        old.release()


async def test_bug_ops_2_metric_labels_stay_bounded_for_hostile_http_methods():
    """BUG-OPS-2: `hive_http_requests_total` was labelled with the raw request method, which a
    client chooses freely (any token is a valid HTTP method), so unauthenticated requests could
    create unbounded metric series (memory growth, a scrape that never ends)."""
    from hive_mcp.ops.metrics import Metrics
    from hive_mcp.ops.observe import ObserveMiddleware

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 405, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(_message):
        return None

    async def receive():
        return {"type": "http.request"}

    metrics = Metrics()
    middleware = ObserveMiddleware(app, metrics)
    for method in ("GET", "POST", *(f"X{i}" for i in range(50))):
        scope = {"type": "http", "path": "/x", "method": method, "headers": []}
        await middleware(scope, receive, send)
    methods = {
        s.labels["method"]
        for family in metrics.registry.collect()
        if family.name == "hive_http_requests"
        for s in family.samples
        if s.name.endswith("_total")
    }
    assert methods == {"GET", "POST", "OTHER"}


@pytest.fixture
def empty_pg_database():
    from hashlib import sha1

    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = "hive_reg_" + sha1(os.urandom(8)).hexdigest()[:8]  # noqa: S324 - a name
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**conninfo_to_dict(DSN), "dbname": name})
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@needs_pg
def test_bug_ops_3_migrate_does_not_print_the_database_password(
    tmp_path, monkeypatch, empty_pg_database
):
    """BUG-OPS-3: `hive migrate` echoed the target URL as typed, and `describe()` (used in the
    messages of both directions) kept the password because `make_conninfo(password=None)` does not
    remove one: the secret reached the terminal and any CI log that captured it."""
    import re

    from typer.testing import CliRunner

    from hive_mcp.adapters.sqlite.db import Database
    from hive_mcp.cli import cli

    # CI's server needs its real password; a local one trusts any, so use a made-up one there
    found = re.search(r"password=(\S+)", empty_pg_database)
    secret = found.group(1) if found else "hunter2-s3cret"
    url = empty_pg_database if found else f"{empty_pg_database} password={secret}"

    def leaked(text: str) -> bool:  # the secret may equal another word (CI: user and password)
        return (
            f"password={secret}" in text
            or f":{secret}@" in text
            or (secret not in {"postgres"} and secret in text)
        )

    source = tmp_path / "src"
    source.mkdir()
    Database(source / "hive.db").close()
    monkeypatch.setenv("HIVE_DATA_DIR", str(source))
    monkeypatch.delenv("HIVE_DATABASE_URL", raising=False)
    up = CliRunner().invoke(cli, ["migrate", "to-postgres", "--database-url", url, "--no-reindex"])
    assert up.exit_code == 0, up.output
    assert not leaked(up.output)
    monkeypatch.setenv("HIVE_PROFILE", "external")
    monkeypatch.setenv("HIVE_DATABASE_URL", url)
    monkeypatch.setenv("HIVE_SECRET_KEY", "k" * 40)
    down = CliRunner().invoke(
        cli, ["migrate", "to-sqlite", "--data-dir", str(tmp_path / "out"), "--no-reindex"]
    )
    assert down.exit_code == 1 or "from PostgreSQL" in down.output  # source is not empty
    assert not leaked(down.output)


@needs_pg
def test_bug_ops_5_backups_of_an_external_postgres_follow_its_changes(tmp_path, empty_pg_database):
    """BUG-OPS-5: with HIVE_BACKUP_DIR and an external Postgres the loop wrote one archive and then
    never another (the change check only knew SQLite files and the embedded cluster), so the
    "archive every N minutes if something changed" promise silently stopped after the first one."""
    from hive_mcp.config import Settings
    from hive_mcp.ops.autobackup import archives, backup_once

    with psycopg.connect(empty_pg_database, autocommit=True) as conn:
        conn.execute("CREATE TABLE notes (v text)")
        conn.execute("INSERT INTO notes VALUES ('first')")
    settings = Settings(
        profile="external",
        database_url=empty_pg_database,
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "share",
    )
    last = backup_once(settings, None, time.time())
    assert len(archives(settings.backup_dir)) == 1
    time.sleep(1.1)  # archive names carry the second
    with psycopg.connect(empty_pg_database, autocommit=True) as conn:
        conn.execute("INSERT INTO notes VALUES ('second')")
    backup_once(settings, last, time.time())
    assert len(archives(settings.backup_dir)) == 2


@needs_pg
async def test_bug_ops_6_the_final_backup_of_a_postgres_does_not_trust_lagging_counters(
    tmp_path, empty_pg_database, monkeypatch
):
    """BUG-OPS-6: whether Postgres changed is read from pg_stat_database, which a busy backend
    publishes up to ~10 s late (measured on PostgreSQL 16: a second write within a second of the
    previous flush stays invisible for 10 s). A stop in that window skipped the final backup and
    lost the newest rows. The final backup of a Postgres is now unconditional."""
    from hive_mcp.config import Settings
    from hive_mcp.ops import autobackup
    from hive_mcp.ops.autobackup import archives, backup_loop

    with psycopg.connect(empty_pg_database, autocommit=True) as conn:
        conn.execute("CREATE TABLE notes (v text)")
    settings = Settings(
        profile="external",
        database_url=empty_pg_database,
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "share",
    )
    monkeypatch.setattr(autobackup, "pg_activity", lambda dsn: (1, 0, 0))  # a stale reading
    task = asyncio.create_task(backup_loop(settings))
    for _ in range(100):  # the first round runs at once
        if archives(settings.backup_dir):
            break
        await asyncio.sleep(0.1)
    assert len(archives(settings.backup_dir)) == 1
    await asyncio.sleep(1.1)  # archive names carry the second
    with psycopg.connect(empty_pg_database, autocommit=True) as conn:
        conn.execute("INSERT INTO notes VALUES ('newest')")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(archives(settings.backup_dir)) == 2


def test_bug_ops_7_a_restore_that_cannot_complete_keeps_the_existing_database(tmp_path):
    """BUG-OPS-7: `restore --force` deleted the live database (and its WAL) before it looked for
    the replacement inside the archive; an archive whose manifest is consistent but that holds no
    database ended in a raw FileNotFoundError and no database at all."""
    import hashlib
    import io
    import tarfile

    from hive_mcp.config import Settings
    from hive_mcp.domain.errors import ValidationError
    from hive_mcp.ops.backup import restore_backup

    token = b"{}"
    manifest = {
        "format": 1,
        "profile": "embedded",
        "files": {"state/tokens": {"sha256": hashlib.sha256(token).hexdigest(), "size": 2}},
    }
    archive = tmp_path / "no-db.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, payload in (
            ("manifest.json", json.dumps(manifest).encode()),
            ("state/tokens", token),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    data = tmp_path / "data"
    data.mkdir()
    (data / "hive.db").write_bytes(b"precious")
    with pytest.raises(ValidationError, match="no database"):
        restore_backup(Settings(data_dir=data), archive, force=True)
    assert (data / "hive.db").read_bytes() == b"precious"


async def test_bug_ops_8_the_backup_loop_is_stopped_after_every_other_writer(tmp_path, monkeypatch):
    """BUG-OPS-8: shutdown cancels the background loops in list order and each is awaited; the
    backup loop (whose last act is the final backup) was started before the connector loop, so a
    connector sync kept writing during and after the final backup, and what it wrote was lost."""
    from types import SimpleNamespace

    from hive_mcp.config import Settings
    from hive_mcp.connectors import spec as spec_module
    from hive_mcp.ops import autobackup, loops

    stops: list[str] = []

    def loop_named(name):
        async def run(*_args, **_kwargs):
            try:
                await asyncio.sleep(60)
            finally:
                stops.append(name)

        return run

    for name in ("purge_loop", "usage_loop", "connector_loop"):
        monkeypatch.setattr(loops, name, loop_named(name))
    monkeypatch.setattr(autobackup, "backup_loop", loop_named("backup_loop"))
    monkeypatch.setattr(spec_module, "parse_specs", lambda raw: [object()])
    services = SimpleNamespace(digest_sender=None)
    settings = Settings(backup_dir=tmp_path / "share", group_sync_minutes=0)
    tasks = loops.start_loops(services, settings)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    for task in tasks:  # what the application does at shutdown
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stops[-1] == "backup_loop" and "connector_loop" in stops


def test_bug_ops_9_backups_leave_out_the_temporary_files_of_blob_writes(tmp_path):
    """BUG-OPS-9: the blob store writes through `tempfile.mkstemp` (names like `tmpab12cd`), but the
    backup only skipped names ending in `.tmp`. A write in progress (or a leftover of a crash) was
    archived with a hash taken while the file was still changing, so the archive could fail its own
    verification (a failed final backup), or carry junk into every restore."""
    import asyncio as aio
    import sqlite3
    import tarfile

    from hive_mcp.adapters.fs.blobs import FsBlobs
    from hive_mcp.config import Settings
    from hive_mcp.ops.backup import create_backup, verify_backup

    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "hive.db").close()
    ref = aio.run(FsBlobs(data / "blobs").put("acme", b"real bytes"))
    leftover = next((data / "blobs").rglob(ref.sha256)).parent / "tmpab12cd34"
    leftover.write_bytes(b"half a wri")
    summary = create_backup(Settings(data_dir=data), tmp_path / "out.tar.gz")
    with tarfile.open(summary.path) as tar:
        names = tar.getnames()
    assert not any(n.endswith("tmpab12cd34") for n in names), names
    assert any(n.endswith(ref.sha256) for n in names)
    verify_backup(summary.path)


def test_bug_ops_10_a_state_file_rewritten_during_a_backup_does_not_break_its_checksum(
    tmp_path, monkeypatch
):
    """BUG-OPS-10: tokens.json / users.json are rewritten while the server runs, but the backup
    hashed them in place and added them to the tar later. A change in between produced an archive
    that failed its own verification (for the final backup at shutdown: no final backup)."""
    import sqlite3

    from hive_mcp.config import Settings
    from hive_mcp.ops import backup as backup_module

    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "hive.db").close()
    tokens = data / "tokens.json"
    tokens.write_text("[]")
    real = backup_module.sha256_file

    def hash_then_change(path):
        digest = real(path)
        if "tokens" in path.name:
            tokens.write_text('[{"a": 1}]')  # a token is created right after the file was hashed
        return digest

    monkeypatch.setattr(backup_module, "sha256_file", hash_then_change)
    summary = backup_module.create_backup(Settings(data_dir=data), tmp_path / "x.tar.gz")
    monkeypatch.setattr(backup_module, "sha256_file", real)
    backup_module.verify_backup(summary.path)


@needs_pg
async def test_bug_ops_11_readiness_fails_when_the_postgres_is_gone(empty_pg_database):
    """BUG-OPS-11: docs/SINGLE-CONTAINER.md promises that when the embedded Postgres dies the
    readiness check fails; /readyz only looked at the lifecycle flags and stayed 200 while every
    request failed."""
    import httpx
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from starlette.applications import Starlette

    from hive_mcp.config import Settings
    from hive_mcp.ops.health import health_routes
    from hive_mcp.ops.serve import storage_probe
    from hive_mcp.ops.state import Lifecycle

    async def readyz(dsn: str) -> int:
        lifecycle = Lifecycle()
        lifecycle.mark_ready()
        lifecycle.probe = storage_probe(Settings(profile="external", database_url=dsn))
        transport = httpx.ASGITransport(app=Starlette(routes=health_routes(lifecycle)))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return (await client.get("/readyz")).status_code

    assert await readyz(empty_pg_database) == 200
    gone = make_conninfo(**{**conninfo_to_dict(empty_pg_database), "dbname": "no_such_db_" + "x"})
    assert await readyz(gone) == 503
    assert storage_probe(Settings()) is None  # SQLite: a file, nothing to ask


async def test_bug_ops_12_a_scrape_during_a_refresh_never_sees_emptied_job_gauges():
    """BUG-OPS-12: `Metrics.refresh` cleared the job gauges and then awaited the database, so a
    second scrape arriving meanwhile (an HA pair of Prometheus servers) rendered no `hive_jobs`
    series at all, and a failing query left stale vector gauges beside cleared job gauges."""
    from types import SimpleNamespace

    from hive_mcp.ops.metrics import Metrics
    from hive_mcp.ports.jobs import JobStat, JobState

    gate = asyncio.Event()

    async def stats():
        await gate.wait()
        return [JobStat("index", JobState.DEAD, 3, 0.0)]

    async def status():
        return SimpleNamespace(coverage=1.0, indexable_items=1, current_items=1)

    services = SimpleNamespace(
        backend=SimpleNamespace(jobs=SimpleNamespace(stats=stats)),
        vector_admin=SimpleNamespace(status=status),
    )
    metrics = Metrics()
    metrics.jobs.labels("index", "dead").set(3)  # the previous scrape's numbers
    refreshing = asyncio.create_task(metrics.refresh(services))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert b'hive_jobs{kind="index",state="dead"} 3.0' in metrics.render()  # not emptied meanwhile
    gate.set()
    await refreshing
    assert b'hive_jobs{kind="index",state="dead"} 3.0' in metrics.render()
