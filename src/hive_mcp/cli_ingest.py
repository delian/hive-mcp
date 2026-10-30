"""Operator commands that read files into the knowledge base: ingest, ingest-git and the
background bulk ingest (ingest --background, ingest-status, ingest-cancel)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ingest.files import files_under

app = typer.Typer()


def public_url(url: str) -> str:
    """The address without `user:token@` (it is stored as items' source, which readers can see)."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url  # also scp-style git@host:org/repo, where `git` is a user name, not a secret
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


@app.command()
def ingest(  # noqa: PLR0913, PLR0917 - one option per identity claim
    paths: list[Path] = typer.Argument(..., exists=True, help="Files or directories."),  # noqa: B008
    org: str = typer.Option(..., help="Organisation id."),
    user: str = typer.Option(..., help="User id that will own the knowledge."),
    role: str = typer.Option("member", help="Role of that user."),
    project: str | None = typer.Option(None, help="Share into this project (user must be in it)."),
    tag: list[str] = typer.Option([], help="Tag for every file (repeatable)."),  # noqa: B008
    background: bool = typer.Option(
        False, "--background", help="Queue ONE server-side folder as a resumable job and return."
    ),
) -> None:
    """Ingest files (recursively for directories) as the given user. Safe to re-run."""
    if background:
        run_cli(lambda: _enqueue(paths, org, user, project, tag))
        return

    async def run() -> int:
        services = await build_services(Settings())
        who = Principal(
            user, org, Role[role.upper()], frozenset(), frozenset({project} if project else ())
        )
        scope = Scope(ScopeLevel.PROJECT, project) if project else None
        failures = 0
        try:
            for path in files_under(paths):
                try:
                    done = await services.ingest.ingest(
                        who, path.name, path.read_bytes(), tags=tuple(tag), scope=scope
                    )
                except (HiveError, OSError) as exc:
                    failures += 1
                    typer.echo(f"FAILED {path}: {exc}", err=True)
                    continue
                typer.echo(f"{path}: {len(done.items)} parts, {done.created} new")
        finally:
            await services.close()
        return failures

    try:
        failed = asyncio.run(run())
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if failed:
        raise typer.Exit(1)


@app.command("ingest-git")
def ingest_git(  # noqa: PLR0913, PLR0917 - one option per identity claim
    url: str = typer.Argument(..., help="Repository address (https://..., git@host:org/repo.git)."),
    org: str = typer.Option(..., help="Organisation id."),
    user: str = typer.Option(..., help="User id that will own the knowledge."),
    ref: str | None = typer.Option(None, help="Branch or tag (default: the repository's default)."),
    project: str | None = typer.Option(None, help="Share into this project (user must be in it)."),
    tag: list[str] = typer.Option([], help="Tag for every file (repeatable)."),  # noqa: B008
    exclude: list[str] = typer.Option([], help="Extra glob to skip (repeatable)."),  # noqa: B008
) -> None:
    """Read a repository's documents and code as knowledge (shallow clone; nothing is executed)."""
    import tempfile  # noqa: PLC0415

    from hive_mcp.ingest.git import DEFAULT_EXCLUDES, clone, repo_name, select_files  # noqa: PLC0415

    async def run() -> int:
        services = await build_services(Settings())
        who = Principal(
            user, org, Role.MEMBER, frozenset(), frozenset({project} if project else ())
        )
        scope = Scope(ScopeLevel.PROJECT, project) if project else None
        name, failures = repo_name(url), 0
        try:
            with tempfile.TemporaryDirectory(prefix="hive-git-") as tmp:
                root = Path(tmp) / "repo"
                clone(url, root, ref)
                files, skipped = select_files(root, (*DEFAULT_EXCLUDES, *exclude))
                typer.echo(f"{len(files)} files to read, {skipped} skipped")
                for path in files:
                    rel = path.relative_to(root).as_posix()
                    try:
                        done = await services.ingest.ingest(
                            who, rel, path.read_bytes(), tags=(*tag, f"repo/{name}"), scope=scope,
                            source=f"git:{public_url(url)}#{rel}"[:400],
                        )  # fmt: skip
                    except (HiveError, OSError) as exc:
                        failures += 1
                        typer.echo(f"FAILED {rel}: {exc}", err=True)
                        continue
                    typer.echo(f"{rel}: {len(done.items)} parts, {done.created} new")
        finally:
            await services.close()
        return failures

    try:
        failed = asyncio.run(run())
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if failed:
        raise typer.Exit(1)


async def _enqueue(
    paths: list[Path], org: str, user: str, project: str | None, tag: list[str]
) -> None:
    if len(paths) != 1 or not paths[0].is_dir():
        raise HiveError("--background takes exactly one folder")
    services = await build_services(Settings())
    try:
        assert services.bulk_ingest  # noqa: S101 - always built
        folder = services.bulk_ingest.check_root(paths[0])
        run_id = await services.bulk_ingest.start(
            folder, org, user, project=project, tags=tuple(tag)
        )
    finally:
        await services.close()
    typer.echo(f"queued bulk ingest {run_id} for {folder}")
    typer.echo("The server reads this path itself: it must exist inside the server's container.")
    typer.echo(f"The running server works through it; follow with: hive ingest-status {run_id}")


@app.command("ingest-status")
def ingest_status(
    job_id: str | None = typer.Argument(None, help="One bulk ingest (default: list them)."),
    org: str | None = typer.Option(None, help="Organisation id (needed to list)."),
) -> None:
    """Progress of background folder ingests: state, files done/failed/skipped, bytes, errors."""

    async def run() -> None:
        services = await build_services(Settings())
        try:
            assert services.bulk_ingest  # noqa: S101 - always built
            if job_id:
                found = await services.bulk_ingest.status(job_id)
                runs = [found] if found else []
            elif org:
                runs = await services.bulk_ingest.list_runs(org)
            else:
                raise HiveError("give a JOB_ID or --org")
        finally:
            await services.close()
        if not runs:
            typer.echo("no such bulk ingest" if job_id else "no bulk ingests")
        for r in runs:
            p = r.progress
            typer.echo(
                f"{r.run_id}  {r.state:<10} {r.root}  files {p.done} done, {p.failed} failed, "
                f"{p.skipped} skipped, {p.created} new items, {p.bytes} bytes  "
                f"last: {p.cursor or '-'}  started {r.started}"
            )
            if job_id:
                for line in p.errors:
                    typer.echo(f"  {line}")

    run_cli(run)


@app.command("ingest-cancel")
def ingest_cancel(job_id: str = typer.Argument(..., help="Bulk ingest id.")) -> None:
    """Stop a background folder ingest after the file in progress (what is stored stays)."""

    async def run() -> None:
        services = await build_services(Settings())
        try:
            assert services.bulk_ingest  # noqa: S101 - always built
            stopped = await services.bulk_ingest.cancel(job_id)
        finally:
            await services.close()
        if not stopped:
            raise HiveError("unknown or already finished")
        typer.echo(f"cancelling {job_id}")

    run_cli(run)
