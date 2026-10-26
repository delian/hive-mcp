"""Commands for the machine an agent runs on: `hive hook <event>` and `hive hooks install|remove|...`."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path  # noqa: TC003 - typer reads the annotation at runtime

import typer

from hive_hooks import hooks, install
from hive_hooks.install import HooksError

app = typer.Typer()
hooks_app = typer.Typer(no_args_is_help=True, help="Connect coding agents to the knowledge base.")
app.add_typer(hooks_app, name="hooks")


@app.command("hook", hidden=True)
def hook(
    event: str = typer.Argument(..., help="session-start, prompt, pre-compact, session-end"),
) -> None:
    """Run by Claude Code, not by people: reads the event on stdin; never fails and never blocks."""
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    output = asyncio.run(hooks.run(event, stdin))
    if output:
        typer.echo(output)


def _fail(exc: HooksError) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1) from exc


@hooks_app.command("install")
def hooks_install(
    scope: str = typer.Option(
        "user", help="user (all projects), project (shared in git) or local."
    ),
    command: str = typer.Option(
        None,
        help='How Claude Code starts the CLI, e.g. "uvx --from hive-hooks hive-hooks" (default: this program).',
    ),
    prompt_hook: bool = typer.Option(
        True, help="Search with every substantial prompt (--no-prompt-hook: off)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the resulting file without writing it."
    ),
) -> None:
    """Add the session hooks to Claude Code's settings. Needs HIVE_URL and HIVE_TOKEN where it runs."""
    try:
        path = install.settings_path(scope)
        merged = install.merge(
            install.load(path),
            command=command or install.default_command(),
            prompt_hook=prompt_hook,
        )
    except HooksError as exc:
        _fail(exc)
        return
    if dry_run:
        import json  # noqa: PLC0415

        typer.echo(f"# {path}\n{json.dumps(merged, indent=2)}")
        return
    install.save(path, merged)
    typer.echo(f"hooks installed in {path}: {', '.join(install.installed_events(merged))}")
    typer.echo(
        "Set HIVE_URL (the /mcp address) and HIVE_TOKEN in the environment Claude Code starts from;"
    )
    typer.echo(
        'add the server itself with:  claude mcp add --transport http hive $HIVE_URL --header "Authorization: Bearer $HIVE_TOKEN"'
    )


@hooks_app.command("remove")
def hooks_remove(scope: str = typer.Option("user", help="user, project or local.")) -> None:
    """Take our hooks out again; other hooks and settings stay."""
    try:
        path = install.settings_path(scope)
        settings = install.load(path)
    except HooksError as exc:
        _fail(exc)
        return
    if not install.installed_events(settings):
        typer.echo(f"no hive hooks in {path}")
        return
    install.save(path, install.remove(settings))
    typer.echo(f"hooks removed from {path}")


@hooks_app.command("status")
def hooks_status() -> None:
    """Which scopes have hooks, and whether this machine can reach the server."""
    from hive_hooks.connection import NotConfigured, call_tool  # noqa: PLC0415

    for scope in ("user", "project", "local"):
        try:
            events = install.installed_events(install.load(install.settings_path(scope)))
        except HooksError as exc:
            events = [f"unreadable ({exc})"]
        typer.echo(f"{scope:<8} {', '.join(events) or '-'}")
    try:
        asyncio.run(call_tool("kb_tags", {"limit": 1}))
        typer.echo("server   reachable, token accepted")
    except NotConfigured as exc:
        typer.echo(f"server   not configured: {exc}")
    except Exception as exc:
        typer.echo(f"server   NOT reachable: {exc.__class__.__name__}")


@hooks_app.command("snippet")
def hooks_snippet(
    file: Path | None = typer.Option(  # noqa: B008
        None, help="Write it into this file (AGENTS.md, CLAUDE.md, GEMINI.md)."
    ),
    remove: bool = typer.Option(False, help="Take the note out of --file again."),
) -> None:
    """The instructions note for agents that read AGENTS.md / CLAUDE.md; idempotent between markers."""
    if file is None:
        typer.echo(install.SNIPPET, nl=False)
        return
    text = file.read_text() if file.exists() else ""
    file.write_text(install.strip_snippet(text) if remove else install.apply_snippet(text))
    typer.echo(f"{'removed from' if remove else 'written to'} {file}")


def main() -> None:
    app()
