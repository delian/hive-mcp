"""`hive-hooks init`: everything a new machine or project needs, in one command."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import typer

from hive_hooks import editors, install
from hive_hooks.install import HooksError


def _save_token(token: str) -> Path:
    from hive_hooks.connection import TOKEN_FILE  # noqa: PLC0415

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE


def _editor_list(value: str) -> list[str]:
    names = list(editors.EDITORS) if value == "all" else [v for v in value.split(",") if v]
    for name in names:
        if name not in editors.EDITORS:
            raise HooksError(f"--editor: {name!r} is not one of {', '.join(editors.EDITORS)}, all")
    return names


def _check() -> None:
    from hive_hooks.connection import NotConfigured, call_tool  # noqa: PLC0415

    try:
        asyncio.run(call_tool("kb_tags", {"limit": 1}))
        typer.echo("server   reachable, token accepted")
    except NotConfigured as exc:
        typer.echo(f"server   not checked: {exc}")
    except Exception as exc:
        typer.echo(f"server   NOT reachable: {exc.__class__.__name__}")


def init(  # noqa: PLR0913, PLR0917 - one option per decision the command offers
    url: str = typer.Option("", envvar="HIVE_URL", help="The server's /mcp address."),
    editor: str = typer.Option("claude", help="claude, vscode, cursor (comma separated) or all."),
    scope: str = typer.Option("user", help="Where the Claude Code hooks go: user, project, local."),
    root: Path = typer.Option(Path(), help="The project the editor files go into."),  # noqa: B008
    agents_file: Path | None = typer.Option(  # noqa: B008
        None, help="Also put the instructions note into this file (AGENTS.md, CLAUDE.md)."
    ),
    save_token: bool = typer.Option(
        False, "--save-token", help="Store HIVE_TOKEN (or a prompted one) in ~/.config/hive/token."
    ),
    hooks: bool = typer.Option(True, help="Install the Claude Code hooks (--no-hooks: skip)."),
    check: bool = typer.Option(True, help="Try the server at the end (--no-check: skip)."),
) -> None:
    """Hooks, editor MCP entries and the agents note, without a token in any project file."""
    try:
        if not url:
            raise HooksError("give --url (or set HIVE_URL): the server's /mcp address")
        names = _editor_list(editor)
        if save_token:
            token = os.environ.get("HIVE_TOKEN", "").strip() or (
                typer.prompt("Hive token", hide_input=True) if sys.stdin.isatty() else ""
            )
            if not token:
                raise HooksError("--save-token needs HIVE_TOKEN in the environment or a terminal")
            typer.echo(f"token stored in {_save_token(token)} (owner-only)")
        if hooks:
            path = install.settings_path(scope)
            merged = install.merge(install.load(path), command=install.default_command())
            install.save(path, merged)
            typer.echo(f"hooks    {path}")
        for name in names:
            path = editors.path_for(name, root)
            install.save(path, editors.merge(name, install.load(path), url))
            typer.echo(f"{name:<8} {path}")
        if agents_file:
            text = agents_file.read_text() if agents_file.exists() else ""
            agents_file.write_text(install.apply_snippet(text))
            typer.echo(f"note     {agents_file}")
    except HooksError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("The token itself stays out of these files: set HIVE_TOKEN where the tool starts.")
    if check:
        _check()
