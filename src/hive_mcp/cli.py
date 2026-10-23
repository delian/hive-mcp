"""`hive` command line entry point."""

from __future__ import annotations

import typer
import uvicorn

from hive_mcp import __version__
from hive_mcp.app import create_app
from hive_mcp.config import Settings

cli = typer.Typer(no_args_is_help=True, add_completion=False)


@cli.command()
def serve() -> None:
    """Run the MCP server (settings from HIVE_* env vars)."""
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=int(settings.drain_seconds),
    )


@cli.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def main() -> None:
    cli()
