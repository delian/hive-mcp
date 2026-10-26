"""Operator commands that change or inspect the data: reindex, erase, evaluate."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.domain.principal import Principal, Role

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

    run_cli(run)


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

    run_cli(run)


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

    run_cli(run)
