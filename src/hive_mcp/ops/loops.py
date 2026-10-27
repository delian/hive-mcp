"""Background loops of the server process: housekeeping, group sync and the weekly digest."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.clock import SystemClock
from hive_mcp.domain.errors import HiveError, StorageBusy
from hive_mcp.ops.usage import FLUSH_SECONDS, METER
from hive_mcp.security.groups.factory import group_sync_configured, run_group_sync

if TYPE_CHECKING:
    from hive_mcp.bootstrap import Services
    from hive_mcp.config import Settings
    from hive_mcp.connectors.spec import ConnectorSpec

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
            if services.retention and services.retention.rules:
                await services.retention.run()


async def usage_loop(services: Services) -> None:
    """Save the per-person tool-call counts every few minutes (and once more at shutdown)."""
    clock = SystemClock()
    try:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await METER.flush(services.backend.audit, clock)
    finally:
        with contextlib.suppress(Exception):
            await METER.flush(services.backend.audit, clock)


CONNECTOR_CHECK_SECONDS = 60
RUN_NS = "connector:run"


async def connector_loop(
    services: Services, settings: Settings, specs: list[ConnectorSpec]
) -> None:
    """Runs each connector once per interval slot; a claim in the key-value store keeps it to one
    replica per slot, and a restart inside a slot does not repeat the run."""
    import time  # noqa: PLC0415

    from hive_mcp.connectors.factory import make_connector  # noqa: PLC0415

    assert services.connectors is not None  # noqa: S101 - built by bootstrap
    while True:
        for spec in specs:
            slot = int(time.time() // (spec.every_minutes * 60))
            claimed = await services.backend.kv.put_if_absent(
                RUN_NS,
                f"{spec.name}:{slot}",
                {"claimed": True},
                ttl_seconds=spec.every_minutes * 120,
            )
            if not claimed:
                continue
            try:
                report = await services.connectors.run(spec, make_connector(spec, settings))
                log.info("connector.sync name=%s %s", spec.name, report.line())
            except Exception:  # one bad source (network, parser) must not stop the loop
                log.exception("connector.failed name=%s", spec.name)
        await asyncio.sleep(CONNECTOR_CHECK_SECONDS)


def start_loops(services: Services, settings: Settings) -> list[asyncio.Task[None]]:
    """The server's background loops, all inside this process; cancel them at shutdown."""
    from hive_mcp.connectors.spec import parse_specs  # noqa: PLC0415

    tasks = [
        asyncio.create_task(purge_loop(services, settings.retention_session_days)),
        asyncio.create_task(usage_loop(services)),
    ]
    if services.digest_sender and settings.digest_weekday is not None:
        tasks.append(
            asyncio.create_task(
                digest_loop(services, settings.digest_weekday, settings.digest_hour_utc)
            )
        )
    if settings.group_sync_minutes and group_sync_configured(settings):
        tasks.append(asyncio.create_task(group_sync_loop(settings)))
    specs = parse_specs(settings.connectors)
    if specs and settings.run_jobs:
        tasks.append(asyncio.create_task(connector_loop(services, settings, specs)))
    if settings.backup_dir is not None:  # last: shutdown cancels in this order, and the final
        from hive_mcp.ops.autobackup import backup_loop  # noqa: PLC0415  backup must see every
        #                                                  other writer already stopped

        tasks.append(asyncio.create_task(backup_loop(settings)))
    return tasks
