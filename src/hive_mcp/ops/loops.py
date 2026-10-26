"""Background loops of the server process: housekeeping, group sync and the weekly digest."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import HiveError, StorageBusy
from hive_mcp.security.groups.factory import run_group_sync

if TYPE_CHECKING:
    from hive_mcp.bootstrap import Services
    from hive_mcp.config import Settings

log = logging.getLogger("hive.app")
PURGE_INTERVAL_SECONDS = 600
DIGEST_CHECK_SECONDS = 600


async def group_sync_loop(settings: Settings) -> None:
    """Failures are logged and retried next round; a sync is all-or-nothing, never half applied."""
    while True:
        try:
            changes = await run_group_sync(settings)
            log.info("groups.sync_done changes=%d", len(changes))
        except HiveError:
            log.exception("groups.sync_failed")
        await asyncio.sleep(settings.group_sync_minutes * 60)


async def digest_loop(services: Services, weekday: int, hour: int) -> None:
    """Once a week at the chosen time, email everyone their digest. Claims in the key-value
    store keep it to one email per person per week across replicas and restarts."""
    sender, done_week = services.digest_sender, None
    while sender:
        now = SystemClock().now()
        week = now.isocalendar()[:2]
        if now.weekday() == weekday and now.hour >= hour and week != done_week:
            try:
                sent, failed = await sender.send_all()
                log.info("digest.sent sent=%d failed=%d", sent, failed)
                done_week = week if not failed else None  # failures are retried at the next check
            except HiveError:
                log.exception("digest.failed")
        await asyncio.sleep(DIGEST_CHECK_SECONDS)


async def purge_loop(services: Services, retention_days: int = 0) -> None:
    """Expired records are already invisible; this reclaims their space and applies retention."""
    while True:
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        with contextlib.suppress(StorageBusy):
            await services.backend.kv.purge_expired()
            await services.purge.sweep(retention_days)
