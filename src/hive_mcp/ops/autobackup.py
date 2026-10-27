"""Self-contained durability: the server backs itself up, and restores itself on an empty disk.

For deployments of ONE container with no database service, where the container's own disk is lost
when it is replaced (Azure Container Apps, Cloud Run, a plain `docker run` without a volume): set
HIVE_BACKUP_DIR to durable storage (an Azure Files share, a mounted volume). Every few minutes, if
anything changed, the server writes a verified backup archive there and keeps the newest few; when
it starts with no database but with backups, it restores the newest valid one first. The data lives
on fast local disk (SQLite must not sit on a network share) and the share holds only archives.
The price is the interval: a crash loses at most the changes since the last archive. A final backup
is taken at shutdown, so planned restarts and upgrades lose nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hive_mcp.config import Profile
from hive_mcp.domain.errors import HiveError
from hive_mcp.ops.backup import create_backup, restore_backup, verify_backup

if TYPE_CHECKING:
    from pathlib import Path

    from hive_mcp.config import Settings

log = logging.getLogger("hive.backup")
PREFIX = "hive-auto-"
SUFFIX = ".tar.gz"


def archives(directory: Path) -> list[Path]:
    """Complete automatic archives, oldest first (the names carry the UTC time)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file())


def restore_if_empty(settings: Settings) -> Path | None:
    """On a disk without a database, restore the newest backup that verifies. Returns it."""
    if (
        settings.backup_dir is None
        or settings.profile is not Profile.EMBEDDED  # Postgres keeps its own data
        or (settings.data_dir / "hive.db").exists()
    ):
        return None
    for archive in reversed(archives(settings.backup_dir)):
        try:
            verify_backup(archive)
            restore_backup(settings, archive)
        except HiveError as exc:
            log.error("backup %s unusable (%s); trying an older one", archive.name, exc)
            continue
        log.warning("restored the database from %s", archive.name)
        return archive
    return None


def _changed_since(settings: Settings, since: float) -> bool:
    """Has the database (or its write-ahead log) been touched after the last backup began?"""
    names = ("hive.db", "hive.db-wal", "tokens.json", "users.json")
    return any((p := settings.data_dir / n).exists() and p.stat().st_mtime > since for n in names)


def backup_once(settings: Settings, last_start: float | None, now: float) -> float | None:
    """Write, verify and prune. Returns the start time to remember, or None if nothing was due."""
    assert settings.backup_dir is not None  # noqa: S101 - callers check
    if last_start is not None and not _changed_since(settings, last_start):
        return last_start
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{datetime.now(UTC):%Y%m%d-%H%M%S}{SUFFIX}"
    summary = create_backup(settings, settings.backup_dir / name)
    verify_backup(summary.path)  # an archive nobody has read back is not a backup
    for old in archives(settings.backup_dir)[: -settings.backup_keep]:
        old.unlink(missing_ok=True)
    log.info("backup written: %s (%d files)", summary.path.name, summary.files)
    return now


async def backup_loop(settings: Settings) -> None:
    import time  # noqa: PLC0415

    last: float | None = None
    try:
        while True:
            started = time.time()
            try:
                last = await asyncio.to_thread(backup_once, settings, last, started)
            except (HiveError, OSError):
                log.exception("backup failed; will try again")
            await asyncio.sleep(settings.backup_every_minutes * 60)
    finally:
        try:  # the last words of a stopping server: planned restarts lose nothing
            await asyncio.to_thread(backup_once, settings, last, time.time())
        except (HiveError, OSError):
            log.exception("final backup failed")
