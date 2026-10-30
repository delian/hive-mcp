"""`hive export`: write an organisation's knowledge as JSONL or Parquet for analytics."""

from __future__ import annotations

import getpass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import ValidationError
from hive_mcp.export import ExportOptions, run_export

app = typer.Typer()


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("--since must be an ISO date or time, e.g. 2026-01-31") from exc
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@app.command("export")
def export_command(  # noqa: PLR0913, PLR0917 - one option per documented flag
    org: str = typer.Option(..., help="Organisation id."),
    out: Path = typer.Option(..., help="Directory to create (must not exist or be empty)."),  # noqa: B008
    format: str = typer.Option("jsonl", help="jsonl, or parquet (needs the [parquet] extra)."),
    include_private: bool = typer.Option(
        False, "--include-private/--no-include-private", help="Also export PRIVATE notes."
    ),
    include_archived: bool = typer.Option(False, help="Also export archived items."),
    since: str = typer.Option(None, help="Only items changed (links created) on/after this date."),
    pseudonymise: bool = typer.Option(
        False, help="Replace person ids with stable salted hashes (valid inside this export only)."
    ),
) -> None:
    """Export items, links and a checksummed manifest for analytics (files are mode 0600)."""

    async def run() -> None:
        opts = ExportOptions(
            format, include_private, include_archived, parse_since(since), pseudonymise
        )
        services = await build_services(Settings())
        try:
            b = services.backend
            result = await run_export(
                items=b.items,
                maintenance=b.maintenance,
                edges=b.edges,
                audit=b.audit,
                clock=SystemClock(),
                org_id=org,
                out_dir=out,
                opts=opts,
                actor=f"cli:{getpass.getuser()}",
            )
        finally:
            await services.close()
        if include_private:
            typer.secho(
                "WARNING: this export contains PRIVATE notes. Protect it like the database.",
                fg=typer.colors.RED,
                bold=True,
            )
        typer.echo(f"exported {result.items} items and {result.edges} links to {result.path}")

    run_cli(run)
