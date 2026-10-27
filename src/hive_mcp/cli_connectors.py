"""`hive connectors`: list, run now, or show the last result of the configured connectors."""

from __future__ import annotations

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.connectors.factory import make_connector
from hive_mcp.connectors.spec import parse_specs
from hive_mcp.connectors.sync import STATUS_NS
from hive_mcp.domain.errors import ValidationError

app = typer.Typer(no_args_is_help=True, help="Scheduled imports (HIVE_CONNECTORS).")


@app.command("list")
def connectors_list() -> None:
    """The configured connectors and how often each runs."""
    specs = parse_specs(Settings().connectors)
    if not specs:
        typer.echo("no connectors (set HIVE_CONNECTORS, see docs/CONNECTORS.md)")
    for s in specs:
        typer.echo(f"{s.name}\t{s.type}\tevery {s.every_minutes} min\tinto project {s.project}")


@app.command("status")
def connectors_status() -> None:
    """The result of each connector's last run."""

    async def run() -> None:
        settings = Settings()
        services = await build_services(settings)
        try:
            for spec in parse_specs(settings.connectors):
                last = await services.backend.kv.get(STATUS_NS, spec.name)
                typer.echo(
                    f"{spec.name}: {last['summary'] + ' at ' + last['at'] if last else 'never ran'}"
                )
                for doc, why in (last or {}).get("failed", {}).items():
                    typer.echo(f"  failed {doc}: {why}")
        finally:
            await services.close()

    run_cli(run)


@app.command("run")
def connectors_run(
    name: str = typer.Argument(None, help="Only this connector (default: all)."),
    force: bool = typer.Option(False, help="Retire documents even if most of them vanished."),
) -> None:
    """Sync now, once, instead of waiting for the schedule."""

    async def run() -> None:
        settings = Settings()
        specs = [s for s in parse_specs(settings.connectors) if name in (None, s.name)]
        if not specs:
            raise ValidationError("no such connector" if name else "no connectors configured")
        services = await build_services(settings)
        try:
            assert services.connectors is not None  # noqa: S101
            for spec in specs:
                report = await services.connectors.run(
                    spec, make_connector(spec, settings), allow_mass_retire=force
                )
                typer.echo(f"{spec.name}: {report.line()}")
                for doc, why in report.failed.items():
                    typer.echo(f"  failed {doc}: {why}", err=True)
        finally:
            await services.close()

    run_cli(run)
