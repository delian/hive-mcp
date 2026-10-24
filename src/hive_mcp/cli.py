"""`hive` command line entry point."""

from __future__ import annotations

import time

import typer
import uvicorn

from hive_mcp import __version__
from hive_mcp.app import create_app
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.tokens import TokenStore

cli = typer.Typer(no_args_is_help=True, add_completion=False)
tokens = typer.Typer(
    no_args_is_help=True, help="Manage static bearer tokens (pipelines, CLI, tests)."
)
cli.add_typer(tokens, name="token")


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


@tokens.command("create")
def token_create(  # noqa: PLR0913, PLR0917 - one option per claim
    label: str = typer.Option(..., help="Unique name, used to revoke the token."),
    subject: str = typer.Option(..., help="User or service id the token acts as."),
    org: str = typer.Option(..., help="Organisation id."),
    role: str = typer.Option("member", help="viewer, member, editor or admin."),
    team: list[str] = typer.Option([], help="Team membership (repeatable)."),  # noqa: B008
    project: list[str] = typer.Option([], help="Project membership (repeatable)."),  # noqa: B008
    expires_days: float | None = typer.Option(
        None, help="Token lifetime in days (default: never)."
    ),
) -> None:
    """Create a token. The plaintext is printed once and cannot be recovered."""
    try:
        principal = Principal(subject, org, Role[role.upper()], frozenset(team), frozenset(project))
        token = TokenStore(Settings().tokens_path).create(
            principal, label=label, expires_in_days=expires_days
        )
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(token)
    typer.echo("Store this token now; only its hash is kept.", err=True)


@tokens.command("list")
def token_list() -> None:
    """List tokens (labels and claims only, never secrets)."""
    for r in TokenStore(Settings().tokens_path).list():
        expiry = (
            "never"
            if r.expires_at is None
            else time.strftime("%Y-%m-%d", time.gmtime(r.expires_at))
        )
        org, role = r.claims.get("hive_org"), r.claims.get("hive_role")
        typer.echo(f"{r.label}\t{r.subject}\t{org}\t{role}\texpires {expiry}")


@tokens.command("revoke")
def token_revoke(label: str) -> None:
    """Revoke a token by label; takes effect immediately, no restart needed."""
    if not TokenStore(Settings().tokens_path).revoke(label):
        typer.echo(f"no token labelled {label!r}", err=True)
        raise typer.Exit(1)
    typer.echo(f"revoked {label}")


def main() -> None:
    cli()
