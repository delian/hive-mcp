"""`hive` command line entry point."""

from __future__ import annotations

import time

import typer
import uvicorn

from hive_mcp import __version__
from hive_mcp.app import create_app
from hive_mcp.cli_ops import app as ops_app
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import parse_scope, scope_key
from hive_mcp.embeddings.models import fetch_model, get_spec
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.state import Lifecycle
from hive_mcp.security.directory import DirectoryUser, UserDirectory, normalize_email
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.security.tokens import TokenStore

cli = typer.Typer(no_args_is_help=True, add_completion=False)
tokens = typer.Typer(
    no_args_is_help=True, help="Manage static bearer tokens (pipelines, CLI, tests)."
)
cli.add_typer(tokens, name="token")
users = typer.Typer(
    no_args_is_help=True, help="Manage who may sign in (Google accounts) and what they belong to."
)
cli.add_typer(users, name="user")
models = typer.Typer(no_args_is_help=True, help="Manage local embedding models.")
cli.add_typer(models, name="models")
cli.add_typer(ops_app)  # data commands (ingest, reindex, forget, purge, eval, ...)


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
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
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
    approves: list[str] = typer.Option(  # noqa: B008
        [], help="Scope whose promotions this token approves, e.g. team:t1 (repeatable)."
    ),
    expires_days: float | None = typer.Option(
        None, help="Token lifetime in days (default: never)."
    ),
) -> None:
    """Create a token. The plaintext is printed once and cannot be recovered."""
    try:
        principal = Principal(
            subject,
            org,
            Role[role.upper()],
            frozenset(team),
            frozenset(project),
            frozenset(parse_scope(a) for a in approves),
        )
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


def _directory() -> UserDirectory:
    return UserDirectory(AtomicJsonFile(Settings().users_path, default=[]))


@users.command("add")
def user_add(  # noqa: PLR0913, PLR0917 - one option per attribute
    email: str = typer.Argument(..., help="The person's Google Workspace email address."),
    org: str = typer.Option(..., help="Organisation id."),
    role: str = typer.Option("member", help="viewer, member, editor or admin."),
    team: list[str] = typer.Option([], help="Team membership (repeatable)."),  # noqa: B008
    project: list[str] = typer.Option([], help="Project membership (repeatable)."),  # noqa: B008
    disabled: bool = typer.Option(False, help="Keep the entry but block sign-in and live tokens."),
    approves: list[str] = typer.Option(  # noqa: B008
        [], help="Scope whose promotions they approve, e.g. team:t1 (repeatable)."
    ),
) -> None:
    """Add or update a user. Changes apply to live tokens immediately."""
    try:
        user = DirectoryUser(
            normalize_email(email),
            org,
            Role[role.upper()],
            frozenset(team),
            frozenset(project),
            disabled,
            frozenset(scope_key(parse_scope(a)) for a in approves),
        )
        _directory().upsert(user)
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"saved {user.email}")


@users.command("list")
def user_list() -> None:
    """List users."""
    for u in _directory().list():
        flags = " (disabled)" if u.disabled else ""
        typer.echo(
            f"{u.email}\t{u.org_id}\t{u.role.name.lower()}\tteams={sorted(u.teams)}\tprojects={sorted(u.projects)}\tapproves={sorted(u.approves)}{flags}"
        )


@users.command("remove")
def user_remove(email: str) -> None:
    """Remove a user; their tokens stop working immediately."""
    if not _directory().remove(email):
        typer.echo(f"no such user {email!r}", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed {email}")


def main() -> None:
    cli()


@models.command("fetch")
def models_fetch(
    name: str = typer.Argument("bge-small-en-v1.5-int8", help="Pinned model name."),
) -> None:
    """Download and checksum-verify a local embedding model (needed once for HIVE_EMBEDDER=onnx)."""
    try:
        settings = Settings()
        model, _ = fetch_model(get_spec(name), settings.models_path)
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ready: {model.parent}")
