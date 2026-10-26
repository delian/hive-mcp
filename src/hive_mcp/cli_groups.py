"""`hive sync-groups`: apply Google Workspace group membership to the user directory."""

from __future__ import annotations

import typer

from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.security.groups.factory import run_group_sync

app = typer.Typer()


@app.command("sync-groups")
def sync_groups_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the changes without writing them."),
    allow_empty: bool = typer.Option(
        False, "--allow-empty", help="Accept a group that returns no members."
    ),
) -> None:
    """Make teams, projects, approvers and roles match the groups in HIVE_GROUP_MAP_FILE."""

    async def run() -> None:
        changes = await run_group_sync(Settings(), dry_run=dry_run, allow_empty=allow_empty)
        for c in changes:
            after = c.after
            typer.echo(
                f"{c.action}\t{c.email}\t{after.role.name.lower()}\tteams={sorted(after.teams)}"
                f"\tprojects={sorted(after.projects)}\tapproves={sorted(after.approves)}"
            )
        typer.echo(f"{len(changes)} change(s){' (dry run, nothing written)' if dry_run else ''}")

    run_cli(run)
