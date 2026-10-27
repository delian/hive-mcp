"""Starting the server process: lease, restore on an empty disk, then uvicorn until SIGTERM."""

from __future__ import annotations

import os
import signal

import uvicorn

from hive_mcp.app import create_app
from hive_mcp.config import Profile, Settings
from hive_mcp.ops.autobackup import restore_if_empty
from hive_mcp.ops.drain import DrainingServer
from hive_mcp.ops.lease import Lease
from hive_mcp.ops.logs import configure_logging
from hive_mcp.ops.state import Lifecycle


def _stop_self() -> None:
    """A graceful stop, exactly as if the platform had sent SIGTERM: final backup, then release."""
    os.kill(os.getpid(), signal.SIGTERM)


def run_server(settings: Settings) -> None:
    configure_logging(settings.log_format, settings.log_level)
    lease = None
    if settings.backup_dir is not None and settings.profile is Profile.EMBEDDED:
        lease = Lease(  # one writer at a time (docs/SINGLE-CONTAINER.md)
            settings.backup_dir,
            revision=settings.effective_revision if settings.backup_handoff else None,
            on_handoff=_stop_self,
        )
        lease.acquire(settings.backup_lease_wait)
        restore_if_empty(settings)  # a replaced container comes back with its data
    lifecycle = Lifecycle()
    config = uvicorn.Config(
        create_app(settings, lifecycle, on_stop=lease.release if lease else None),
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
        if lease:  # also when startup failed; a second release is harmless
            lease.release()
