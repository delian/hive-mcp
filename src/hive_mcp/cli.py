"""`hive` command line entry point."""

from __future__ import annotations

import typer
import uvicorn

from hive_mcp import __version__
from hive_mcp.app import create_app
from hive_mcp.config import Settings
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.state import Lifecycle

cli = typer.Typer(no_args_is_help=True, add_completion=False)


@cli.command()
def serve() -> None:
    """Run the MCP server (settings from HIVE_* env vars)."""
    settings = Settings()
    lifecycle = Lifecycle()
    config = uvicorn.Config(
        create_app(settings, lifecycle),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=int(settings.shutdown_timeout),
    )
    DrainingServer(config, lifecycle, settings.drain_seconds).run()


@cli.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def main() -> None:
    cli()
