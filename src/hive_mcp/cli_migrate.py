"""`hive migrate to-postgres | to-sqlite`: move the knowledge base between the two profiles."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Profile, Settings
from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.ops.migrate.run import Side, migrate, plan
from hive_mcp.ops.migrate.sqlite_side import SqliteSide
from hive_mcp.ops.migrate.tables import TABLES

app = typer.Typer(no_args_is_help=True, help="Move the data between SQLite and PostgreSQL.")
STATE_FILES = ("tokens.json", "users.json", "secret.key")


def _copy_state(source: Path, target: Path) -> list[str]:
    """Tokens, users and the cookie/secret key live in files next to the database."""
    if source.resolve() == target.resolve():
        return []
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in STATE_FILES:
        if (source / name).is_file() and not (target / name).exists():
            shutil.copy2(source / name, target / name)
            (target / name).chmod(0o600)
            copied.append(name)
    return copied


async def _index(settings: Settings, orgs: list[str]) -> int:
    services = await build_services(settings)
    try:
        total = 0
        for org in orgs:
            after = None
            while ids := await services.backend.items.list_ids_system(org, after, 200):
                for item_id in ids:
                    await services.indexing.index_current(org, item_id)
                total, after = total + len(ids), ids[-1]
        return total
    finally:
        await services.close()


def _report(source: Side, target: Side | None, name: str, reindex: Settings | None) -> None:
    """Dry run when `target` is None: only count, write nothing."""
    typer.echo(f"from {source.describe()}\n  to {name}")
    if target is None:
        counts = {t.name: source.count(t.name) for t in TABLES}
        typer.echo("rows: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        typer.echo("dry run: nothing was written")
        return
    typer.echo("rows: " + ", ".join(f"{k} {v}" for k, v in plan(source, target).items()))
    result = migrate(source, target, progress=typer.echo)
    if result.skipped_expired:
        typer.echo(f"left out {result.skipped_expired} expired records")
    if not result.verified:
        for problem in result.problems:
            typer.echo(f"PROBLEM: {problem}", err=True)
        raise typer.Exit(1)
    typer.echo("verified: both stores read back identical (counts and checksums)")
    if reindex is not None:
        typer.echo("indexing for search (this embeds every note)...")
        n = asyncio.run(_index(reindex, source.orgs()))
        typer.echo(f"indexed {n} notes")
    else:
        typer.echo("search indexes not built: run `hive reindex --org <id>` with the server up")


@app.command("to-postgres")
def to_postgres(
    database_url: str = typer.Option("", help="The target PostgreSQL (empty database, pgvector)."),
    embedded: bool = typer.Option(
        False,
        "--embedded",
        help="Target the PostgreSQL inside this container (WITH_POSTGRES image).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only count; write nothing."),
    reindex: bool = typer.Option(True, help="Build the search indexes now (--no-reindex: later)."),
) -> None:
    """Copy this SQLite deployment (HIVE_DATA_DIR) into PostgreSQL. Stop the server first."""
    from hive_mcp.ops.migrate.pg_side import PgSide, redact_dsn  # noqa: PLC0415 - postgres extra

    settings = Settings()
    try:
        if settings.profile is not Profile.EMBEDDED or settings.embedded_postgres:
            raise ValidationError("the source is the SQLite profile: unset HIVE_DATABASE_URL")
        if bool(database_url) == embedded:
            raise ValidationError("give exactly one of --database-url and --embedded")
        pg = None
        if embedded and not dry_run:
            from hive_mcp.config_extras import EMBEDDED_PG_DSN  # noqa: PLC0415
            from hive_mcp.ops.embedded_pg import EmbeddedPostgres  # noqa: PLC0415

            pg = EmbeddedPostgres(settings.data_dir)
            pg.start()
            database_url = EMBEDDED_PG_DSN
        elif embedded:
            database_url = "the PostgreSQL inside this container"
        try:
            source = SqliteSide(settings.data_dir, target=False)
            target = None if dry_run else PgSide(database_url, target=True)
            try:
                target_settings = (
                    Settings(embedded_postgres=True, data_dir=settings.data_dir)
                    if embedded
                    else Settings(
                        profile=Profile.EXTERNAL, database_url=database_url,
                        data_dir=settings.data_dir,
                    )
                )  # fmt: skip
                shown = database_url if embedded else redact_dsn(database_url)
                _report(source, target, shown, target_settings if reindex else None)
            finally:
                source.close()
                if target:
                    target.close()
        finally:
            if pg:
                pg.stop()
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("to-sqlite")
def to_sqlite(
    data_dir: Path = typer.Option(..., help="An empty directory for the SQLite deployment."),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Only count; write nothing."),
    reindex: bool = typer.Option(True, help="Build the search indexes now (--no-reindex: later)."),
) -> None:
    """Copy the PostgreSQL deployment (HIVE_DATABASE_URL) into a SQLite directory."""
    from hive_mcp.ops.migrate.pg_side import PgSide  # noqa: PLC0415 - postgres extra

    settings = Settings()
    try:
        if settings.profile is not Profile.EXTERNAL or not settings.database_url:
            raise ValidationError("the source is PostgreSQL: set HIVE_DATABASE_URL")
        source = PgSide(settings.database_url, target=False)
        try:
            target = None if dry_run else SqliteSide(data_dir, target=True)
            try:
                if not dry_run:
                    for name in _copy_state(settings.data_dir, data_dir):
                        typer.echo(f"copied {name}")
                after = Settings(profile=Profile.EMBEDDED, data_dir=data_dir)
                _report(source, target, str(data_dir), after if reindex else None)
            finally:
                if target:
                    target.close()
        finally:
            source.close()
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
