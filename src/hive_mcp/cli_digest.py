"""`hive digest`: preview a person's digest, or send the weekly emails now."""

from __future__ import annotations

import typer

from hive_mcp.bootstrap import build_services
from hive_mcp.cli_common import run_cli
from hive_mcp.config import Settings
from hive_mcp.domain.errors import ValidationError
from hive_mcp.security.directory import UserDirectory, normalize_email
from hive_mcp.security.jsonfile import AtomicJsonFile
from hive_mcp.services.digest import render_text

app = typer.Typer()


@app.command("digest")
def digest_command(
    user: str = typer.Option(
        None, help="Show this person's digest (their email, as in `hive user list`)."
    ),
    days: int = typer.Option(7, help="How many days back."),
    send_now: bool = typer.Option(
        False, "--send", help="Email everyone's digest now (needs SMTP)."
    ),
) -> None:
    """Show what is new for a person, or send this week's digest emails right away."""

    async def run() -> None:
        settings = Settings()
        services = await build_services(settings)
        try:
            if send_now:
                if services.digest_sender is None:
                    raise ValidationError(
                        "emailing needs HIVE_SMTP_HOST/FROM and HIVE_DIGEST_WEEKDAY"
                    )
                sent, failed = await services.digest_sender.send_all()
                typer.echo(
                    f"sent {sent}, failed {failed} (people already sent this week are skipped)"
                )
                return
            if not user:
                raise ValidationError("give --user EMAIL to preview, or --send")
            directory = UserDirectory(AtomicJsonFile(settings.users_path, default=[]))
            person = directory.lookup(normalize_email(user))
            if person is None:
                raise ValidationError(f"no active user {user!r}")
            typer.echo(
                render_text(
                    await services.digests.build(person.principal(), days=days), person.email
                )
            )
        finally:
            await services.close()

    run_cli(run)
