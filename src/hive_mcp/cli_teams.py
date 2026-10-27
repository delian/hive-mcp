"""`hive teams-package`: the Teams app package for this server."""

from __future__ import annotations

from pathlib import Path

import typer

from hive_mcp.config import Settings
from hive_mcp.domain.errors import HiveError
from hive_mcp.teams.manifest import package

app = typer.Typer()


@app.command("teams-package")
def teams_package(
    app_id: str = typer.Option(..., help="The bot's application (client) id from Azure."),
    out: Path = typer.Option(Path("hive-teams.zip"), help="Where to write the package."),  # noqa: B008
    name: str = typer.Option("Hive", help="The app's name in Teams."),
    version: str = typer.Option("1.0.0", help="Raise it when you upload a changed package."),
    link_unfurling: bool | None = typer.Option(
        None,
        "--link-unfurling/--no-link-unfurling",
        help="Unfurl pasted note links (default: on when HIVE_ADMIN_UI is on, which serves them).",
    ),
) -> None:
    """Write the Teams app package (manifest, icons) for HIVE_PUBLIC_URL; upload it in Teams."""
    try:
        settings = Settings()
        data = package(
            app_id, settings.public_url, name=name, version=version,
            privacy_url=settings.privacy_url, terms_url=settings.terms_url,
            link_unfurling=settings.admin_ui if link_unfurling is None else link_unfurling,
        )  # fmt: skip
    except HiveError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    out.write_bytes(data)
    typer.echo(f"wrote {out} ({len(data)} bytes); upload it in Teams: Apps, Manage your apps")
