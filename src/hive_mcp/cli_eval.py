"""`hive eval-build`: make an evaluation set from real searches (needs HIVE_SEARCH_FEEDBACK)."""

from __future__ import annotations

import json
import os
import random
from datetime import timedelta
from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime
from typing import TYPE_CHECKING, Any

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.clock import SystemClock
from hive_mcp.evaluation import dataset
from hive_mcp.evaluation.feedback import build_set, pair_searches_with_opens
from hive_mcp.evaluation.report import build_report
from hive_mcp.services.feedback import OPEN, QUERY, RATING

if TYPE_CHECKING:
    from hive_mcp.bootstrap import Services
    from hive_mcp.ports.audit import AuditRecord

app = typer.Typer()
PAGE = 500


async def _trail(services: Services, org: str, since: Any) -> list[AuditRecord]:  # noqa: ANN401
    rows: list[AuditRecord] = []
    for action in (QUERY, OPEN, RATING):
        after = 0
        while page := await services.backend.audit.list(
            org, after_seq=after, limit=PAGE, action=action
        ):
            rows += [r for r in page if r.event.at >= since]
            after = page[-1].seq
    return sorted(rows, key=lambda r: r.seq)


async def _distractors(services: Services, org: str, count: int) -> list[tuple[str, str, str]]:
    ids: list[str] = []
    after: str | None = None
    while page := await services.backend.items.list_ids_system(org, after, 1000):
        ids += page
        after = page[-1]
    chosen = random.Random(0).sample(ids, min(count, len(ids)))  # noqa: S311 - a fixed sample, not security
    found = [await services.backend.items.get_system(org, i) for i in chosen]
    return [(i.id, i.title, i.body) for i in found if i and i.status.value != "archived"]


@app.command("eval-build")
def eval_build(
    out: Path = typer.Argument(..., help="Where to write the evaluation set (JSON)."),  # noqa: B008
    org: str = typer.Option(..., help="Organisation id."),
    days: int = typer.Option(30, help="Use searches from the last N days."),
    distractors: int = typer.Option(50, help="Random other items to add as noise."),
    negatives: Path | None = typer.Option(  # noqa: B008
        None, exists=True, help="Take the unrelated ('negative') queries from this set."
    ),
) -> None:
    """Pair searches with the results opened afterwards and write a set for `hive eval`.

    The file contains the text of real items, private ones included: keep it out of version control
    and delete it when done. Review the pairs; people also open results that were not the answer.
    """

    async def run() -> None:
        services = await build_services(Settings())
        try:
            since = SystemClock().now() - timedelta(days=days)
            pairs = pair_searches_with_opens(await _trail(services, org, since))
            wanted = {i for ids in pairs.relevant.values() for i in ids}
            docs = {}
            for item_id in wanted:
                item = await services.backend.items.get_system(org, item_id)
                if item:
                    docs[item_id] = (item.title, item.body)
            extra = [d for d in await _distractors(services, org, distractors)]
        finally:
            await services.close()
        negs = (
            [q.text for q in dataset.load(negatives).queries if not q.relevant] if negatives else []
        )
        built = build_set(f"feedback-{org}", pairs, docs, extra, negs)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(built, handle, indent=1, ensure_ascii=False)
        positives = sum(1 for q in built["queries"] if q["relevant"])
        typer.echo(
            f"{pairs.searches} searches, {pairs.opens} opens and "
            f"{pairs.rated_useful + pairs.rated_useless} ratings in the last {days} days gave "
            f"{positives} labelled queries, {len(negs)} negatives, "
            f"{len(built['docs'])} documents -> {out}"
        )
        if positives < 20:  # noqa: PLR2004
            typer.echo(
                "Few labels so far: results are anecdotes until there are a few dozen.", err=True
            )
        typer.echo(
            "The file holds real item text; keep it private and delete it when done.", err=True
        )

    run_cli(run)


@app.command("feedback-report")
def feedback_report(
    org: str = typer.Option(..., help="Organisation id."),
    days: int = typer.Option(30, help="Use searches from the last N days."),
) -> None:
    """Questions the knowledge base did not answer, and which notes helped or misled."""

    async def run() -> None:
        services = await build_services(Settings())
        try:
            since = SystemClock().now() - timedelta(days=days)
            report = build_report(await _trail(services, org, since))
        finally:
            await services.close()
        typer.echo(f"{report.searches} searches in {days} days, {report.answered} answered")
        for title, rows in (
            ("Found nothing (notes to write)", report.empty),
            ("Found something, nothing helped", report.gaps),
            ("Most useful notes (id)", report.useful),
            ("Rated not useful (id)", report.useless),
        ):
            if rows:
                typer.echo(f"\n{title}:")
                for text, n in rows:
                    typer.echo(f"  {n:>3}  {text}")

    run_cli(run)


@app.command("eval-assist")
def eval_assist(
    what: str = typer.Argument(..., help="conflicts, merges or ranking."),
    file: Path = typer.Argument(  # noqa: B008
        ..., help="evals/conflicts.json, evals/merges.json or a retrieval set."
    ),
) -> None:
    """Score contradiction checks, merge proposals or ranking nudges (a model: HIVE_LLM_*)."""
    from hive_mcp.evaluation import assist_eval  # noqa: PLC0415
    from hive_mcp.llm.factory import make_llm  # noqa: PLC0415

    if what not in {"conflicts", "merges", "ranking"}:
        raise typer.BadParameter("what must be conflicts, merges or ranking")
    settings = Settings()
    llm = make_llm(settings)
    raw = json.loads(file.read_text())

    async def run() -> None:
        if what == "conflicts":
            for r in await assist_eval.evaluate_conflicts(raw, llm):
                typer.echo(
                    f"{r.name:<24} precision {r.precision:.2f} recall {r.recall:.2f} F1 {r.f1:.2f} "
                    f"(tp {r.tp} fp {r.fp} fn {r.fn} tn {r.tn})"
                )
            if llm is None:
                typer.echo("(no model configured: set HIVE_LLM_PROVIDER to score it too)")
        elif what == "merges":
            if llm is None:
                raise typer.BadParameter("merges need a model: set HIVE_LLM_PROVIDER")
            for m in await assist_eval.evaluate_merges(raw, llm):
                note = f", missing {m.missing}" if m.missing else ""
                typer.echo(
                    f"group {m.group}: {m.kept}/{m.total} facts kept{note} {m.error}".rstrip()
                )
        else:
            from hive_mcp.embeddings.factory import make_embedder  # noqa: PLC0415

            for p in await assist_eval.replay_votes(dataset.parse(raw), make_embedder(settings)):
                typer.echo(f"{p.label:<30} MRR {p.mrr:.3f}  R@1 {p.recall_at_1:.3f}")

    run_cli(run)
