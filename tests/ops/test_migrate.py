"""Moving a knowledge base between SQLite and PostgreSQL, both ways, with a proof it is identical."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from hive_mcp.bootstrap import build_services
from hive_mcp.cli import cli
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import ItemPatch
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.ops.migrate.run import migrate, plan
from hive_mcp.ops.migrate.sqlite_side import SqliteSide
from hive_mcp.ops.migrate.tables import TABLES, Fingerprint
from hive_mcp.services.models import ConsultRequest, RememberRequest

DSN = os.environ.get("HIVE_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="HIVE_TEST_PG_DSN not set")
ANN = Principal("ann@acme.com", "acme", Role.MEMBER, projects=frozenset({"p1"}))
ADMIN = Principal("admin@acme.com", "acme", Role.ADMIN)
runner = CliRunner()


async def populate(settings: Settings) -> list[str]:
    """A small but varied knowledge base: scopes, tags, a changed note, a link, a file, state."""
    services = await build_services(settings)
    try:
        notes = [
            await services.knowledge.remember(
                ANN,
                RememberRequest(
                    title="Rollback runbook",
                    body="run make rollback pelican",
                    tags=("ops/deploy",),
                    scope=Scope(ScopeLevel.PROJECT, "p1"),
                ),
            ),
            await services.knowledge.remember(
                ANN, RememberRequest(title="Private idea", body="try the walrus approach")
            ),
            await services.knowledge.remember(
                ADMIN,
                RememberRequest(
                    title="Holiday policy",
                    body="25 days per year",
                    scope=Scope(ScopeLevel.ORG, "acme"),
                ),
            ),
        ]
        first = notes[0].item
        await services.knowledge.update(
            ANN, first.id, ItemPatch(body="run make rollback, then check healthz pelican"), 1
        )
        await services.graph.link(ANN, first.id, notes[1].item.id, "related")
        await services.promotions.request(
            ANN, notes[1].item.id, Scope(ScopeLevel.ORG, "acme"), Visibility.SCOPE, "useful"
        )
        await services.backend.blobs.put("acme", b"the original upload bytes")
        await services.backend.kv.put("integration:config", "slack", {"config": {"x": 1}})
        await services.backend.kv.put("scratch", "gone", {"v": 1}, ttl_seconds=-5)
        return [n.item.id for n in notes]
    finally:
        await services.close()


async def search(settings: Settings, who: Principal, text: str) -> list[str]:
    services = await build_services(settings)
    try:
        found = await services.consult.consult(who, ConsultRequest(text))
        return [m.title for m in found.primary]
    finally:
        await services.close()


def fingerprints(side) -> dict[str, Fingerprint]:
    out = {}
    for table in TABLES:
        fp = Fingerprint()
        for row in side.read(table):
            expires = row.get("expires_at") if table.name == "kv" else None
            if expires is None or expires > datetime.now(UTC):  # expired records are not copied
                fp.add(table, row)
        out[table.name] = fp
    return out


@pytest.fixture
def pg_database():
    """An empty database on the test server, dropped afterwards."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = "hive_mig_" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]  # noqa: S324 - a name
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    base = conninfo_to_dict(DSN)
    dsn = make_conninfo(**{**base, "dbname": name})
    yield dsn
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@needs_pg
def test_sqlite_to_postgres_and_back_keeps_everything_and_search_works(tmp_path, pg_database):
    from hive_mcp.ops.migrate.pg_side import PgSide

    source_dir = tmp_path / "a"
    ids = asyncio.run(populate(Settings(data_dir=source_dir)))
    db_before = (source_dir / "hive.db").read_bytes()

    source, target = SqliteSide(source_dir, target=False), PgSide(pg_database, target=True)
    try:
        report = migrate(source, target, progress=lambda _m: None)
        assert report.verified and report.copied["items"] == 3 and report.copied["blobs"] == 1
        assert report.copied["item_revisions"] >= 1 and report.copied["edges"] == 1
        assert report.copied["promotions"] == 1 and report.skipped_expired == 1
        assert report.copied["kv"] >= 1 and report.copied["audit"] >= 5
        assert fingerprints(source) == fingerprints(target)
    finally:
        source.close()
        target.close()
    assert (source_dir / "hive.db").read_bytes() == db_before  # the source was only read

    on_pg = Settings(profile=Profile.EXTERNAL, database_url=pg_database, data_dir=source_dir)
    asyncio.run(_index(on_pg))
    assert "Rollback runbook" in asyncio.run(search(on_pg, ANN, "make rollback healthz"))
    assert "Private idea" not in asyncio.run(
        search(on_pg, ADMIN, "walrus approach")
    )  # still private
    history = asyncio.run(_history(on_pg, ids[0]))
    assert history == [2, 1]  # both versions came along

    # and back into a new SQLite directory
    back_dir = tmp_path / "b"
    source = PgSide(pg_database, target=False)
    target = SqliteSide(back_dir, target=True)
    try:
        again = migrate(source, target, progress=lambda _m: None)
        assert again.verified
        assert {k: v for k, v in again.copied.items()} == {k: v for k, v in report.copied.items()}
    finally:
        source.close()
        target.close()
    back = Settings(data_dir=back_dir)
    asyncio.run(_index(back))
    assert "Rollback runbook" in asyncio.run(search(back, ANN, "make rollback healthz"))
    assert asyncio.run(_history(back, ids[0])) == [2, 1]


async def _index(settings: Settings) -> None:
    services = await build_services(settings)
    try:
        for org in ("acme",):
            after = None
            while ids := await services.backend.items.list_ids_system(org, after, 100):
                for item_id in ids:
                    await services.indexing.index_current(org, item_id)
                after = ids[-1]
    finally:
        await services.close()


async def _history(settings: Settings, item_id: str) -> list[int]:
    services = await build_services(settings)
    try:
        return [v.revision for v in await services.knowledge.history(ANN, item_id)]
    finally:
        await services.close()


def test_sqlite_to_sqlite_exercises_the_copy_without_a_server(tmp_path):
    """The generic path (reader, writer, proof) between two SQLite directories."""
    asyncio.run(populate(Settings(data_dir=tmp_path / "a")))
    source, target = (
        SqliteSide(tmp_path / "a", target=False),
        SqliteSide(tmp_path / "b", target=True),
    )
    try:
        report = migrate(source, target, progress=lambda _m: None)
        assert report.verified and report.copied["items"] == 3 and report.copied["blobs"] == 1
        for name, fp in fingerprints(source).items():
            assert fp == fingerprints(target)[name], name
    finally:
        source.close()
        target.close()
    assert "Holiday policy" in asyncio.run(_after_index(tmp_path / "b"))


async def _after_index(path: object) -> list[str]:
    settings = Settings(data_dir=path)
    await _index(settings)
    return await search(settings, ANN, "holiday days per year")


def test_a_target_that_holds_data_or_is_the_source_is_refused(tmp_path):
    asyncio.run(populate(Settings(data_dir=tmp_path / "a")))
    asyncio.run(populate(Settings(data_dir=tmp_path / "b")))
    source = SqliteSide(tmp_path / "a", target=False)
    try:
        with pytest.raises(ValidationError, match="same database"):
            plan(source, SqliteSide(tmp_path / "a", target=False))
        with pytest.raises(ValidationError, match="already exists"):
            SqliteSide(tmp_path / "b", target=True)  # a database is already there
    finally:
        source.close()


def test_a_missing_source_is_a_clear_error(tmp_path):
    with pytest.raises(ValidationError, match="no database"):
        SqliteSide(tmp_path / "nothing", target=False)


def test_a_copy_that_lost_a_row_fails_the_proof(tmp_path):
    asyncio.run(populate(Settings(data_dir=tmp_path / "a")))
    source, target = (
        SqliteSide(tmp_path / "a", target=False),
        SqliteSide(tmp_path / "b", target=True),
    )
    real_write = target.write

    def lossy(table, rows):
        real_write(table, rows[:-1] if table.name == "edges" else rows)

    target.write = lossy  # type: ignore[method-assign]
    try:
        report = migrate(source, target, progress=lambda _m: None)
        assert not report.verified and "edges differs" in report.problems[0]
    finally:
        source.close()
        target.close()


def test_corrupted_stored_files_stop_the_copy(tmp_path):
    asyncio.run(populate(Settings(data_dir=tmp_path / "a")))
    blob = next(p for p in (tmp_path / "a" / "blobs").rglob("*") if len(p.name) == 64)
    blob.write_bytes(b"tampered")
    source, target = (
        SqliteSide(tmp_path / "a", target=False),
        SqliteSide(tmp_path / "b", target=True),
    )
    try:
        with pytest.raises(ValidationError, match="corrupted"):
            migrate(source, target, progress=lambda _m: None)
    finally:
        source.close()
        target.close()


def test_the_commands_validate_their_inputs_and_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    asyncio.run(populate(Settings(data_dir=tmp_path / "a")))
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "a"))
    both = runner.invoke(cli, ["migrate", "to-postgres", "--database-url", "x", "--embedded"])
    assert both.exit_code == 1 and "exactly one" in both.output
    neither = runner.invoke(cli, ["migrate", "to-postgres"])
    assert neither.exit_code == 1 and "exactly one" in neither.output
    wrong = runner.invoke(cli, ["migrate", "to-sqlite", "--data-dir", str(tmp_path / "z")])
    assert wrong.exit_code == 1 and "HIVE_DATABASE_URL" in wrong.output
    dry = runner.invoke(
        cli, ["migrate", "to-postgres", "--database-url", "host=nowhere", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert "items 3" in dry.output and "nothing was written" in dry.output
