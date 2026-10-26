"""Operator commands for the stored data's health: vector indexes, backup and restore."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime
from typing import TYPE_CHECKING, Any

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

app = typer.Typer()


vectors = typer.Typer(no_args_is_help=True, help="Inspect and clean up the vector indexes.")
app.add_typer(vectors, name="vectors")


async def _vector_admin(action: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
    services = await build_services(Settings())
    try:
        await action(services.vector_admin)
    finally:
        await services.close()


@vectors.command("status")
def vectors_status() -> None:
    """Show which embedding models have vectors and how complete the current one is."""

    async def show(admin: Any) -> None:  # noqa: ANN401 - VectorAdmin
        s = await admin.status()
        typer.echo(
            f"current model {s.current_model}: {s.current_items}/{s.indexable_items} items "
            f"({s.coverage:.0%})"
        )
        for model, count in sorted(s.other_models.items()):
            typer.echo(
                f"  old model {model}: {count} items (unused; `hive vectors prune` removes it)"
            )

    run_cli(lambda: _vector_admin(show))


@vectors.command("prune")
def vectors_prune(
    yes: bool = typer.Option(False, help="Actually delete (without it, only report)."),
    force: bool = typer.Option(False, help="Delete even if the current model is incomplete."),
) -> None:
    """Delete vectors of embedding models other than the current one."""

    async def prune(admin: Any) -> None:  # noqa: ANN401 - VectorAdmin
        status = await admin.status()
        if not yes:
            for model, count in sorted(status.other_models.items()):
                typer.echo(f"would delete {count} items' vectors of {model}")
            typer.echo(f"current model covers {status.coverage:.0%}; re-run with --yes to delete")
            return
        for model, count in (await admin.prune(force=force)).items():
            typer.echo(f"deleted vectors of {model} ({count} items)")

    run_cli(lambda: _vector_admin(prune))


@app.command()
def backup(
    dest: Path = typer.Argument(..., help="Archive file (.tar.gz) or an existing directory."),  # noqa: B008
) -> None:
    """Back up database, stored logs and state files into one verified archive (live-safe)."""
    from hive_mcp.ops.backup import create_backup, verify_backup  # noqa: PLC0415

    try:
        summary = create_backup(Settings(), dest)
        verify_backup(summary.path)  # a backup nobody has read back is not a backup
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"wrote {summary.path} ({summary.files} files, {summary.bytes} bytes), verified")


@app.command("backup-verify")
def backup_verify(archive: Path = typer.Argument(..., exists=True)) -> None:  # noqa: B008
    """Check an archive's checksums and database integrity without restoring it."""
    from hive_mcp.ops.backup import verify_backup  # noqa: PLC0415

    try:
        summary = verify_backup(archive)
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ok: {summary.profile} backup, {summary.files} files")


@app.command()
def restore(
    archive: Path = typer.Argument(..., exists=True),  # noqa: B008
    force: bool = typer.Option(False, help="Replace existing data."),
) -> None:
    """Restore a backup into this deployment's data directory / database. Stop the server first."""
    from hive_mcp.ops.backup import restore_backup  # noqa: PLC0415

    try:
        summary = restore_backup(Settings(), archive, force=force)
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"restored {summary.files} files from {archive.name}; start the server again")
