"""Operator commands that work on the data itself (ingest, reindex, erase, evaluate)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ingest.files import files_under

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

app = typer.Typer()


@app.command()
def reindex(org: str = typer.Option(..., help="Organisation id.")) -> None:
    """Queue re-embedding of every item (run after changing HIVE_EMBEDDER). The server's job
    runner does the work; a running server need not be restarted."""

    async def run() -> int:
        services = await build_services(Settings())
        try:
            return await services.indexing.reindex_org(org)
        finally:
            await services.close()

    try:
        count = asyncio.run(run())
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"queued {count} items")


@app.command()
def ingest(  # noqa: PLR0913, PLR0917 - one option per identity claim
    paths: list[Path] = typer.Argument(..., exists=True, help="Files or directories."),  # noqa: B008
    org: str = typer.Option(..., help="Organisation id."),
    user: str = typer.Option(..., help="User id that will own the knowledge."),
    role: str = typer.Option("member", help="Role of that user."),
    project: str | None = typer.Option(None, help="Share into this project (user must be in it)."),
    tag: list[str] = typer.Option([], help="Tag for every file (repeatable)."),  # noqa: B008
) -> None:
    """Ingest files (recursively for directories) as the given user. Safe to re-run."""

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


def _admin(org: str) -> Principal:
    return Principal("cli-admin", org, Role.ADMIN)


@app.command()
def forget(
    org: str = typer.Option(..., help="Organisation id."),
    user: str = typer.Option(
        ..., help="User id (the email for Google sign-in) whose knowledge to erase."
    ),
    include_shared: bool = typer.Option(
        False, help="Also erase items they shared with projects/teams."
    ),
    yes: bool = typer.Option(
        False, help="Actually delete (without it, only report what would go)."
    ),
) -> None:
    """Erase a person's private knowledge (and with --include-shared, everything they wrote).

    The audit log and promotion records keep their id; remove the user from the directory and
    revoke their tokens separately (`hive user remove`, `hive token revoke`)."""

    async def run() -> None:
        services = await build_services(Settings())
        try:
            r = await services.purge.forget_user(
                _admin(org), user, include_shared=include_shared, dry_run=not yes
            )
        finally:
            await services.close()
        verb = "erased" if yes else "would erase"
        typer.echo(f"{verb} {len(r.purged)} items; {len(r.kept_shared)} shared items left in place")
        if not yes:
            typer.echo("re-run with --yes to delete")

    _run_cli(run)


@app.command()
def purge(
    item_id: str = typer.Argument(..., help="Item id."),
    org: str = typer.Option(..., help="Organisation id."),
) -> None:
    """Permanently delete one item (operator override of the author/admin rule)."""

    async def run() -> None:
        services = await build_services(Settings())
        try:
            item = await services.purge.purge(_admin(org), item_id)
        finally:
            await services.close()
        typer.echo(f"deleted {item.id}: {item.title}")

    _run_cli(run)


def _run_cli(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    try:
        asyncio.run(coro_fn())
    except (KeyError, HiveError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("eval")
def evaluate_command(
    path: Path = typer.Argument(  # noqa: B008
        ..., exists=True, help="Evaluation set (JSON), e.g. evals/core.json."
    ),
    sweep: bool = typer.Option(False, help="Also sweep the vector relevance floor."),
    floor: float | None = typer.Option(None, help="Override the embedder's relevance floor."),
) -> None:
    """Score keyword, vector and hybrid retrieval with the configured embedder (HIVE_EMBEDDER)."""
    from hive_mcp.embeddings.factory import make_embedder, make_reranker  # noqa: PLC0415
    from hive_mcp.evaluation import dataset, runner  # noqa: PLC0415

    async def run() -> None:
        embedder = make_embedder(Settings())
        data = dataset.load(path)
        points = tuple(x / 100 for x in range(0, 100, 5)) if sweep else ()
        report = await runner.evaluate(data, embedder, floor=floor, sweep=points)
        typer.echo(runner.format_report(report))
        reranker = make_reranker(Settings())
        extra = [await runner.consult_check(data, embedder)]
        if reranker:
            extra.append(await runner.consult_check(data, embedder, reranker))
        for m in extra:  # the full search path, without and with the reranker
            r = m.positives.recall
            typer.echo(
                f"{m.mode:<9} {r[1]:5.2f} {r[3]:5.2f} {r[5]:5.2f} {r[10]:5.2f} "
                f"{m.positives.mrr:5.2f} {m.positives.returned_nothing:6.2f} "
                f"{m.false_positive_rate:7.2f}"
            )

    _run_cli(run)


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

    _run_cli(lambda: _vector_admin(show))


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

    _run_cli(lambda: _vector_admin(prune))


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
                            source=f"git:{url}#{rel}"[:400],
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
