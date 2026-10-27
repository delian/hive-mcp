"""Starting the server process: lease, restore on an empty disk, then uvicorn until SIGTERM."""

from __future__ import annotations

import os
import signal
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
    from collections.abc import Callable


def _stop_self() -> None:
    """A graceful stop, exactly as if the platform had sent SIGTERM: final backup, then release."""
    os.kill(os.getpid(), signal.SIGTERM)


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
