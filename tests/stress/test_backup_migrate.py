"""Backup and migration at size: a snapshot taken while writes continue is consistent and verifies,
and thousands of notes survive SQLite -> Postgres -> SQLite with the built-in proof passing."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import ItemPatch, ItemQuery
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.ops.autobackup import archives, backup_once
from hive_mcp.ops.backup import create_backup, restore_backup, verify_backup
from hive_mcp.ops.migrate.run import migrate
from hive_mcp.ops.migrate.sqlite_side import SqliteSide
from hive_mcp.ops.migrate.tables import TABLES, Fingerprint
from hive_mcp.services.models import ConsultRequest, RememberRequest
from tests.stress.support import SCALE

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.stress
DSN = os.environ.get("HIVE_TEST_PG_DSN")
ANN = Principal("ann@acme.com", "acme", Role.MEMBER, projects=frozenset({"p1"}))
ADMIN = Principal("admin@acme.com", "acme", Role.ADMIN)
P1 = Scope(ScopeLevel.PROJECT, "p1")


async def populate(services, count: int, tag: str = "base") -> list[str]:
    ids: list[str] = []
    for i in range(count):
        r = await services.knowledge.remember(
            ANN,
            RememberRequest(
                f"{tag} note {i}", f"{tag}tok{i} text about pelican numbers {i % 17}", scope=P1
            ),
        )
        ids.append(r.item.id)
    return ids


async def all_ids(services, who: Principal = ANN) -> set[str]:
    from hive_mcp.domain.item import Cursor, Status

    out, after = set(), None
    while True:
        page = await services.knowledge.browse(
            who, ItemQuery(limit=100, after=after, statuses=frozenset(Status))
        )
        out |= {v.item.id for v in page.items}
        if page.next_cursor is None:
            return out
        after = Cursor.decode(page.next_cursor)


async def test_snapshots_taken_during_continuous_writes_are_consistent(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    live = Settings(data_dir=tmp_path / "live")
    services = await build_services(live)
    try:
        base = await populate(services, 800 * SCALE)
        confirmed: list[str] = list(base)
        edited: dict[str, int] = {}
        stop = asyncio.Event()
        writers = 4

        async def writer(n: int) -> None:
            i = 0
            while not stop.is_set():
                r = await services.knowledge.remember(
                    ANN, RememberRequest(f"live {n}-{i}", f"livetok{n}x{i} pelican", scope=P1)
                )
                confirmed.append(r.item.id)
                if i % 3 == 0:  # revisions move too, so history rows must match the snapshot
                    done = await services.knowledge.update(
                        ANN, r.item.id, ItemPatch(body=f"edited livetok{n}x{i} pelican"), 1
                    )
                    edited[r.item.id] = done.revision
                i += 1
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(writer(n)) for n in range(writers)]
        await asyncio.sleep(0.2)  # writes are flowing before the first snapshot
        taken = []
        for k in range(3):
            before = len(confirmed)
            archive = tmp_path / "out" / f"snap{k}.tar.gz"
            t0 = time.perf_counter()
            summary = await asyncio.to_thread(create_backup, live, archive)
            took = time.perf_counter() - t0
            after = len(confirmed)
            await asyncio.to_thread(verify_backup, summary.path)
            taken.append((archive, before, after, took))
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.gather(*tasks)
        assert not list((tmp_path / "out").glob("*.part")), "a half-written archive was left"
        everything = set(confirmed)
    finally:
        await services.close()

    previous: set[str] = set()
    for k, (archive, before, after, _took) in enumerate(taken):
        target = Settings(data_dir=tmp_path / f"restored{k}")
        restore_backup(target, archive)
        restored = await build_services(target)
        try:
            ids = await all_ids(restored)
            # every write confirmed before the snapshot began is in it ...
            assert set(confirmed[:before]) <= ids
            # ... and it holds nothing that was not written (at most the writes still in flight)
            assert ids <= everything
            assert len(ids) <= after + writers
            assert previous <= ids, "a later snapshot lost notes of an earlier one"
            previous = ids
            for item_id in sorted(ids)[:: max(1, len(ids) // 60)]:  # history agrees with the item
                item = (await restored.knowledge.get(ANN, item_id)).item
                revisions = [v.revision for v in await restored.knowledge.history(ANN, item_id)]
                assert revisions == list(range(item.revision, 0, -1)), (item_id, revisions)
                if item_id in edited:
                    assert item.revision in (1, edited[item_id])
            found = await restored.consult.consult(ANN, ConsultRequest("base note pelican"))
            assert found.primary  # the search index travelled with the database
        finally:
            await restored.close()
    print(f"\n[stress] snapshots of a live database took {[round(t[3], 2) for t in taken]} s")


async def test_automatic_backups_prune_and_each_archive_restores(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "live", backup_dir=tmp_path / "share", backup_keep=2)
    services = await build_services(settings)
    try:
        await populate(services, 200 * SCALE)
        last = None
        for round_no in range(3):
            await populate(services, 30, tag=f"r{round_no}")
            last = await asyncio.to_thread(backup_once, settings, last, time.time() - 5)
            await asyncio.sleep(1.05)  # archive names carry the second
            assert last is not None
    finally:
        await services.close()
    kept = archives(settings.backup_dir)
    assert len(kept) == 2, kept
    assert not list(settings.backup_dir.glob("*.part"))
    counts = []
    for archive in kept:
        verify_backup(archive)
        target = Settings(data_dir=tmp_path / f"r-{archive.stem}")
        restore_backup(target, archive)
        restored = await build_services(target)
        try:
            counts.append(len(await all_ids(restored)))
        finally:
            await restored.close()
    assert counts == sorted(counts) and len(set(counts)) == 2, counts  # newer archives hold more


# ---------------------------------------------------------------------------------- migration
async def populate_everything(settings: Settings, count: int) -> list[str]:
    services = await build_services(settings)
    try:
        ids = await populate(services, count)
        for i in range(0, count, 7):  # versions
            await services.knowledge.update(
                ANN, ids[i], ItemPatch(body=f"revised {i} pelican second text"), 1
            )
        for i in range(0, min(count, 300), 5):
            await services.graph.link(ANN, ids[i], ids[i + 1], "related")
        private = [
            (
                await services.knowledge.remember(ANN, RememberRequest(f"mine {i}", f"secret {i}"))
            ).item
            for i in range(10)
        ]
        for note in private[:5]:
            await services.promotions.request(
                ANN, note.id, Scope(ScopeLevel.ORG, "acme"), Visibility.SCOPE, "share"
            )
        for i in range(20):
            await services.backend.blobs.put("acme", f"payload {i} ".encode() * 500)
        for i in range(60):
            await services.backend.kv.put("scratch", f"k{i}", {"v": i})
            await services.backend.kv.put("scratch", f"gone{i}", {"v": i}, ttl_seconds=-5)
        return ids
    finally:
        await services.close()


def fingerprints(side) -> dict[str, Fingerprint]:
    out = {}
    for table in TABLES:
        fp = Fingerprint()
        for row in side.read(table):
            expires = row.get("expires_at") if table.name == "kv" else None
            if expires is None or expires > datetime.now(UTC):
                fp.add(table, row)
        out[table.name] = fp
    return out


@pytest.fixture
def pg_database():
    if not DSN:
        pytest.skip("HIVE_TEST_PG_DSN not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = "hive_mig_" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]  # noqa: S324 - a name
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**conninfo_to_dict(DSN), "dbname": name})
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def reindex(settings: Settings, only: list[str]) -> None:
    """The search indexes are not migrated: rebuild the entries the probes look for."""
    services = await build_services(settings)
    try:
        for item_id in only:
            await services.indexing.index_current("acme", item_id)
    finally:
        await services.close()


def probe_indexes(count: int) -> list[int]:
    """Notes that were never revised, so their original token is still in their text."""
    return [next(i for i in range(start, count) if i % 7) for start in (1, count // 2, count - 3)]


async def probe(settings: Settings, count: int, item_id: str) -> tuple[list[str], list[int]]:
    services = await build_services(settings)
    try:
        titles = []
        for i in probe_indexes(count):
            found = await services.consult.consult(ANN, ConsultRequest(f"basetok{i}", limit=50))
            # the exact token must be among the answers (fuzzy vector matches may outrank it)
            assert f"base note {i}" in [m.title for m in found.primary]
            titles.append(f"base note {i}")
        item = (await services.knowledge.get(ANN, item_id)).item
        history = [v.revision for v in await services.knowledge.history(ANN, item_id)]
        assert history == list(range(item.revision, 0, -1)) and item.revision == 2
        return titles, [len(await all_ids(services, ANN)), len(await all_ids(services, ADMIN))]
    finally:
        await services.close()


def test_thousands_of_notes_sqlite_to_postgres_and_back_with_the_proof(
    tmp_path: Path, pg_database: str
) -> None:
    from hive_mcp.ops.migrate.pg_side import PgSide

    count = 2000 * SCALE
    source_dir = tmp_path / "a"
    ids = asyncio.run(populate_everything(Settings(data_dir=source_dir), count))
    before = (source_dir / "hive.db").read_bytes()
    want_titles, want_counts = asyncio.run(probe(Settings(data_dir=source_dir), count, ids[0]))

    started = time.perf_counter()
    source, target = SqliteSide(source_dir, target=False), PgSide(pg_database, target=True)
    try:
        report = migrate(source, target, progress=lambda _m: None)
        there = time.perf_counter() - started
        assert report.verified and not report.problems
        assert report.copied["items"] == count + 10 and report.copied["blobs"] == 20
        assert report.copied["promotions"] == 5 and report.skipped_expired == 60
        assert report.copied["item_revisions"] >= count // 7
        assert fingerprints(source) == fingerprints(target)
        with pytest.raises(ValidationError, match="already holds data"):
            migrate(source, target, progress=lambda _m: None)  # a second run cannot double up
    finally:
        source.close()
        target.close()
    assert (source_dir / "hive.db").read_bytes() == before  # the source was only read

    on_pg = Settings(profile=Profile.EXTERNAL, database_url=pg_database, data_dir=source_dir)
    asyncio.run(reindex(on_pg, [ids[i] for i in probe_indexes(count)]))
    assert asyncio.run(probe(on_pg, count, ids[0])) == (want_titles, want_counts)

    back_dir = tmp_path / "b"
    started = time.perf_counter()
    source, target = PgSide(pg_database, target=False), SqliteSide(back_dir, target=True)
    try:
        again = migrate(source, target, progress=lambda _m: None)
        assert again.verified and again.copied == report.copied
        first = SqliteSide(source_dir, target=False)
        try:
            assert fingerprints(target) == fingerprints(first)  # SQLite -> PG -> SQLite is identity
        finally:
            first.close()
    finally:
        source.close()
        target.close()
    print(f"\n[stress] {count} notes: sqlite->pg {there:.1f}s, pg->sqlite "
          f"{time.perf_counter() - started:.1f}s")  # fmt: skip


def test_sqlite_to_sqlite_copy_of_thousands_of_notes_verifies(tmp_path: Path) -> None:
    count = 1000 * SCALE
    asyncio.run(populate_everything(Settings(data_dir=tmp_path / "a"), count))
    source, target = (
        SqliteSide(tmp_path / "a", target=False),
        SqliteSide(tmp_path / "b", target=True),
    )
    try:
        t0 = time.perf_counter()
        report = migrate(source, target, progress=lambda _m: None)
        elapsed = time.perf_counter() - t0
        assert report.verified and report.copied["items"] == count + 10
        assert fingerprints(source) == fingerprints(target)
        assert elapsed < 30
    finally:
        source.close()
        target.close()
