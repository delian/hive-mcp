"""Server process, scratch databases and the backup / restore paths, each timed."""

# ruff: noqa: PLR2004, PLR0913, PLR0917

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from hive_mcp.config import Profile, Settings
from hive_mcp.ops.backup import create_backup, restore_backup, verify_backup

METRICS_TOKEN = "loadtest-metrics-token"  # noqa: S105 - a throwaway scratch server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def settings_for(
    backend: str,
    data_dir: Path,
    dsn: str | None,
    embedder: str,
    models: str | None,
    port: int = 8080,
    vector_store: str = "default",
) -> Settings:
    extra: dict[str, Any] = {"embedder": embedder, "vector_store": vector_store}
    if models:
        extra["models_dir"] = Path(models)
    if backend == "postgres":
        extra |= {"profile": Profile.EXTERNAL, "database_url": dsn}
    return Settings(
        data_dir=data_dir,
        host="127.0.0.1",
        port=port,
        public_url=f"http://127.0.0.1:{port}",
        rate_limit_mcp=0,
        metrics_token=METRICS_TOKEN,
        log_level="WARNING",
        **extra,
    )


def scratch_database(admin_dsn: str, name: str) -> str:
    """Create database `name` next to the admin DSN's database, with pgvector; return its DSN."""
    import psycopg  # noqa: PLC0415
    from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: PLC0415

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return dsn


def drop_database(admin_dsn: str, name: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class Server:
    """`hive serve` as a child process, so its memory and start-up time are its own."""

    def __init__(self, settings: Settings, log: Path) -> None:
        env = {**os.environ, **{
            "HIVE_PROFILE": settings.profile.value, "HIVE_DATA_DIR": str(settings.data_dir),
            "HIVE_HOST": "127.0.0.1", "HIVE_PORT": str(settings.port),
            "HIVE_PUBLIC_URL": settings.public_url, "HIVE_RATE_LIMIT_MCP": "0",
            "HIVE_METRICS_TOKEN": METRICS_TOKEN, "HIVE_LOG_LEVEL": "WARNING",
            "HIVE_EMBEDDER": settings.embedder, "HIVE_DRAIN_SECONDS": "0",
        }}  # fmt: skip
        if settings.database_url:
            env["HIVE_DATABASE_URL"] = settings.database_url
        env["HIVE_VECTOR_STORE"] = settings.vector_store
        if settings.models_dir:
            env["HIVE_MODELS_DIR"] = str(settings.models_dir)
        self.url = settings.public_url
        self._log = log.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hive_mcp"], env=env, stdout=self._log, stderr=self._log
        )
        self.started = time.perf_counter()

    def wait_ready(self, timeout: float = 600) -> float:
        """Seconds from spawn until /readyz answers 200."""
        while time.perf_counter() - self.started < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"{self.url}/readyz", timeout=2) as r:  # noqa: S310
                    if r.status == 200:
                        return time.perf_counter() - self.started
            except OSError:
                time.sleep(0.1)
        raise TimeoutError("server did not become ready")

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stop(self) -> float:
        """SIGTERM and wait; returns the seconds the graceful shutdown took."""
        t = time.perf_counter()
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._log.close()
        return time.perf_counter() - t


def backup_cycle(settings: Settings, restore_to: Settings) -> dict[str, Any]:
    """Backup, verify and restore with the product's own code; sizes and seconds."""
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="lt-backup-", dir=settings.data_dir.parent) as tmp:
        t = time.perf_counter()
        summary = create_backup(settings, Path(tmp))
        out["backup_s"] = round(time.perf_counter() - t, 2)
        out["archive_mb"] = round(summary.path.stat().st_size / 1048576, 1)
        out["uncompressed_mb"] = round(summary.bytes / 1048576, 1)
        t = time.perf_counter()
        verify_backup(summary.path)
        out["verify_s"] = round(time.perf_counter() - t, 2)
        t = time.perf_counter()
        restore_backup(restore_to, summary.path, force=True)
        out["restore_s"] = round(time.perf_counter() - t, 2)
    return out


def backup_only(settings: Settings) -> dict[str, Any]:
    """One online backup of the running deployment (what the server's own backup loop does)."""
    with tempfile.TemporaryDirectory(prefix="lt-live-backup-", dir=settings.data_dir.parent) as tmp:
        t = time.perf_counter()
        create_backup(settings, Path(tmp))
        return {"seconds": round(time.perf_counter() - t, 2)}
