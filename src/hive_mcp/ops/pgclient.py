"""Check that pg_dump / pg_restore are not older than the Postgres server they talk to.

An older client refuses with a message about the server version; say the same thing plainly, before
a backup is started, and name what to install.
"""

from __future__ import annotations

import re
import subprocess

from hive_mcp.domain.errors import ValidationError

VERSION = re.compile(r"(\d+)(?:\.\d+)*")
SERVER_VERSION_DIVISOR = 10_000  # server_version_num 160004 -> 16


def client_major(tool: str) -> int | None:
    """The major version a pg_* tool reports, or None if it cannot be told."""
    try:
        out = subprocess.run(  # noqa: S603
            [tool, "--version"], capture_output=True, text=True, check=False, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = VERSION.search(out.split(")", 1)[-1])  # skip the "(PostgreSQL)" label
    return int(found.group(1)) if found else None


def server_major(dsn: str) -> int | None:
    """The server's major version, or None if it cannot be reached (the tool will say why)."""
    try:
        import psycopg  # noqa: PLC0415 - postgres extra

        with psycopg.connect(dsn, connect_timeout=10) as conn:
            row = conn.execute("SHOW server_version_num").fetchone()
        return int(row[0]) // SERVER_VERSION_DIVISOR if row else None
    except Exception:  # any failure here is reported by the tool itself
        return None


def check_client(tool: str, dsn: str) -> None:
    client, server = client_major(tool), server_major(dsn)
    if client is not None and server is not None and client < server:
        raise ValidationError(
            f"{tool.rsplit('/', 1)[-1]} is version {client} but the database server is version "
            f"{server}: install the PostgreSQL {server} client tools "
            f"(for example `apt install postgresql-client-{server}`) and put them first on PATH"
        )
