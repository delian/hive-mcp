"""`hive demo`: one command from an empty container to a working, populated server.

    docker run -p 8080:8080 -v hive:/data hive-mcp hive demo

Creates a demo project, a token and a handful of example notes (only when the knowledge base is
empty), prints how to connect an agent, then serves. Not for production: it prints a token.
"""

from __future__ import annotations

import asyncio

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError, ValidationError
from hive_mcp.domain.item import ItemQuery, Kind
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ops.serve import run_server
from hive_mcp.security.tokens import TokenStore
from hive_mcp.services.models import RememberRequest

app = typer.Typer()
ORG, PROJECT, LABEL = "demo", "demo", "demo"
DEMO = Principal("demo", ORG, Role.ADMIN, frozenset(), frozenset({PROJECT}))
SAMPLES = (
    ("How this knowledge base works", Kind.NOTE, ("topic/hive",),
     "Notes you save are private to you until you share them with a project. Search finds them by "
     "meaning, not only by words. Ask your agent: 'what do we know about releases?'"),
    ("Release checklist", Kind.NOTE, ("topic/release", "kind/checklist"),
     "1. Freeze main and run the full test suite. 2. Tag the release and build the image. "
     "3. Deploy to staging and run the smoke test. 4. Promote to production. 5. Announce in the team channel."),
    ("Runbook: ingest worker stuck", Kind.NOTE, ("topic/ops", "kind/runbook"),
     "Symptom: documents stay in 'queued'. Check `hive connectors status` and the job queue metrics; "
     "restarting the container is safe (jobs are leased and retried). Escalate if the oldest job is over an hour old."),
    ("Decision: share at project level first", Kind.NOTE, ("topic/process", "kind/decision"),
     "Anything wider than a project needs an approver, so teams keep drafts private, share with the project, "
     "and ask for promotion once a note has proven useful."),
    ("Glossary: promotion", Kind.NOTE, ("topic/glossary",),
     "Promotion is a request to share a note with a whole team or the organisation. An approver reviews exactly "
     "the version that was requested."),
)  # fmt: skip


async def seed(settings: Settings) -> tuple[int, str | None]:
    """(notes added, the new token or None when one already exists)."""
    store = TokenStore(settings.tokens_path)
    token = None
    if not any(t.label == LABEL for t in store.list()):
        token = store.create(DEMO, label=LABEL)
    services = await build_services(settings)
    try:
        page = await services.knowledge.browse(DEMO, ItemQuery(limit=1, include_referenced=False))
        if page.items:
            return 0, token
        for title, kind, tags, body in SAMPLES:
            await services.knowledge.remember(
                DEMO,
                RememberRequest(
                    title=title, body=body, kind=kind, tags=tags,
                    scope=Scope(ScopeLevel.PROJECT, PROJECT),
                ),
            )  # fmt: skip
        return len(SAMPLES), token
    finally:
        await services.close()


@app.command()
def demo() -> None:
    """Seed example notes and a token, print how to connect, then serve."""
    settings = Settings()
    try:
        added, token = asyncio.run(seed(settings))
    except (KeyError, HiveError, ValidationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    url = settings.resource_url
    typer.echo(
        f"\nHive demo: {added} example notes in project '{PROJECT}'." if added else "\nHive demo."
    )
    if token:
        typer.echo(f"\nYour token (shown once):\n  {token}\n")
        typer.echo("Connect Claude Code:")
        typer.echo(
            f'  claude mcp add --transport http hive {url} --header "Authorization: Bearer {token}"'
        )
    else:
        typer.echo(f"A token named '{LABEL}' exists; `hive token revoke {LABEL}` to get a new one.")
    typer.echo(f"\nMCP endpoint: {url}\nStop with Ctrl+C. Data is kept in {settings.data_dir}.\n")
    run_server(settings)
