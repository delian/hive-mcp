"""Starting the server process: lease, restore on an empty disk, then uvicorn until SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import TYPE_CHECKING

import uvicorn

from hive_mcp.app import create_app
from hive_mcp.config import Profile, Settings
from hive_mcp.ops.autobackup import restore_if_empty
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.embedded_pg import EmbeddedPostgres
from hive_mcp.ops.lease import Lease
from hive_mcp.ops.logs import configure_logging
from hive_mcp.ops.state import Lifecycle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

PROBE_TIMEOUT = 3.0
PROBE_CACHE_SECONDS = 2.0  # /readyz is public: it must not open a database connection per request


def _stop_self() -> None:
    """A graceful stop, exactly as if the platform had sent SIGTERM: final backup, then release."""
    os.kill(os.getpid(), signal.SIGTERM)


def storage_probe(settings: Settings) -> Callable[[], Awaitable[bool]] | None:
    """Readiness of a Postgres profile (embedded or external): can a connection run a query?

    A dead database otherwise leaves /readyz green while every request fails, so the platform
    never learns the instance is useless (docs/SINGLE-CONTAINER.md). SQLite is a file: no probe."""
    if settings.profile is not Profile.EXTERNAL or not settings.database_url:
        return None
    dsn = settings.database_url
    last: tuple[float, bool] = (float("-inf"), True)

    async def attempt() -> bool:
        import psycopg  # noqa: PLC0415 - postgres extra

        try:
            async with await psycopg.AsyncConnection.connect(
                dsn, connect_timeout=int(PROBE_TIMEOUT), autocommit=True
            ) as conn:
                await conn.execute("SELECT 1")
        except psycopg.Error:
            return False
        return True

    async def probe() -> bool:
        nonlocal last
        now = time.monotonic()
        if now - last[0] < PROBE_CACHE_SECONDS:
            return last[1]
        try:
            ok = await asyncio.wait_for(attempt(), PROBE_TIMEOUT + 1)
        except TimeoutError:
            ok = False
        last = (now, ok)
        return ok

    return probe


def _stopper(lease: Lease | None, pg: EmbeddedPostgres | None) -> Callable[[], None] | None:
    """What runs last: the database stops (after the final backup), then the lease is let go."""
    if lease is None and pg is None:
        return None

    def stop() -> None:
        if pg:
            pg.stop()
        if lease:
            lease.release()

    return stop


def run_server(settings: Settings) -> None:
    configure_logging(settings.log_format, settings.log_level)
    lease = pg = None
    owns_data = settings.profile is Profile.EMBEDDED or settings.embedded_postgres
    if settings.backup_dir is not None and owns_data:
        lease = Lease(  # one writer at a time (docs/SINGLE-CONTAINER.md)
            settings.backup_dir,
            revision=settings.effective_revision if settings.backup_handoff else None,
            on_handoff=_stop_self,
        )
        lease.acquire(settings.backup_lease_wait)
    fresh = False
    if settings.embedded_postgres:
        pg = EmbeddedPostgres(settings.data_dir)
        try:
            fresh = pg.start()
        except BaseException:
            if lease:
                lease.release()
            raise
    if lease:
        restore_if_empty(settings, fresh_pg=fresh)  # a replaced container comes back with its data
    lifecycle = Lifecycle()
    lifecycle.probe = storage_probe(settings)
    stop = _stopper(lease, pg)
    config = uvicorn.Config(
        create_app(settings, lifecycle, on_stop=stop),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=int(settings.shutdown_timeout),
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,  # our handler formats every line, uvicorn's included
    )
    try:
        DrainingServer(config, lifecycle, settings.drain_seconds).run()
    finally:
        if stop:  # also when startup failed; stopping twice is harmless
            stop()
