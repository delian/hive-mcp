"""A PostgreSQL server inside the container (HIVE_EMBEDDED_POSTGRES), for the external profile
without a second service.

The server process owns its life: it starts the cluster before serving and stops it, cleanly, after
the last backup. The cluster listens on a unix socket only (no network port, no password: the
socket directory belongs to the container user) and lives in <data_dir>/pg. A `postmaster.pid`
left by a killed container is stale by construction (only one instance runs, see the lease), and
Postgres can mistake it for a live process when a recycled pid matches, so it is removed first.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from hive_mcp.config_extras import EMBEDDED_PG_SOCKET
from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger("hive.pg")
USER, DATABASE = "hive", "hive"
START_TIMEOUT = 90
LOG_TAIL = 600
PG_ROOT = Path("/usr/lib/postgresql")  # Debian: one directory per major version


def bin_dir() -> Path:
    """The directory with initdb and pg_ctl: on PATH, or Debian's /usr/lib/postgresql/<n>/bin."""
    found = shutil.which("pg_ctl")
    if found:
        return Path(found).parent
    versions = sorted(
        (p for p in PG_ROOT.glob("*/bin") if (p / "pg_ctl").exists()),
        key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 0,
    )
    if not versions:
        raise ValidationError(
            "HIVE_EMBEDDED_POSTGRES needs PostgreSQL in the image: build with WITH_POSTGRES=true"
        )
    return versions[-1]


def _run(cmd: Sequence[str | Path], what: str, log_file: Path | None = None) -> None:
    result = subprocess.run(  # noqa: S603 - fixed commands from our own bin directory
        [str(c) for c in cmd], capture_output=True, text=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        if log_file and log_file.exists():
            detail += " | " + log_file.read_text(errors="replace")[-LOG_TAIL:].strip()
        raise ValidationError(f"embedded Postgres: {what} failed: {detail[-LOG_TAIL:]}")


class EmbeddedPostgres:
    def __init__(self, data_dir: Path, socket_dir: str = EMBEDDED_PG_SOCKET) -> None:
        self.cluster = data_dir / "pg"
        self._socket = Path(socket_dir)
        self._log = data_dir / "pg.log"
        self._started = False

    def start(self) -> bool:
        """Start (creating the cluster on first use). True when the cluster is brand new."""
        binaries = bin_dir()
        os.environ["PATH"] = f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}"  # pg_dump too
        fresh = not (self.cluster / "PG_VERSION").exists()
        if fresh:
            self.cluster.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.cluster.chmod(0o700)
            _run(
                [binaries / "initdb", "-D", self.cluster, "-U", USER, "-E", "UTF8",
                 "--auth-local=trust", "--auth-host=reject"],
                "initdb",
            )  # fmt: skip
        (self.cluster / "postmaster.pid").unlink(missing_ok=True)
        self._socket.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._socket.is_symlink() or self._socket.stat().st_uid != os.getuid():
            raise ValidationError(f"{self._socket} belongs to someone else; remove it first")
        self._socket.chmod(0o700)
        options = f"-c listen_addresses='' -c unix_socket_directories={self._socket}"
        _run(
            [binaries / "pg_ctl", "-D", self.cluster, "-w", "-t", str(START_TIMEOUT),
             "-l", self._log, "-o", options, "start"],
            "start",
            self._log,
        )  # fmt: skip
        self._started = True
        self._ensure_database()
        log.info("embedded Postgres %s at %s", "created" if fresh else "started", self.cluster)
        return fresh

    def _ensure_database(self) -> None:
        import psycopg  # noqa: PLC0415 - postgres extra

        with psycopg.connect(
            f"host={self._socket} dbname=postgres user={USER}", autocommit=True
        ) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
            if exists.fetchone() is None:
                conn.execute(f'CREATE DATABASE "{DATABASE}"')

    def stop(self) -> None:
        """A fast, clean shutdown (every connection closed, checkpoint written). Idempotent."""
        if not self._started:
            return
        self._started = False
        try:
            _run([bin_dir() / "pg_ctl", "-D", self.cluster, "-m", "fast", "-w", "stop"], "stop")
        except ValidationError:
            log.exception("embedded Postgres did not stop cleanly")
