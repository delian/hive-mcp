"""A short soak: a mixed workload over real HTTP for ~12 s must stay error free, keep its latency
and memory flat, and leave no thread, file descriptor or task behind after shutdown.

The long version (minutes, 50k notes, Postgres) is scripts/loadtest.py; see docs/SIZING.md."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING, Any

import pytest
from scripts.loadtest_lib.population import Meta, Vocabulary, ingest, make_users
from scripts.loadtest_lib.report import summarise
from scripts.loadtest_lib.sysmon import proc_counts, rss_mb
from scripts.loadtest_lib.workload import drive

from hive_mcp.bootstrap import build_services
from hive_mcp.config import Settings
from hive_mcp.security.tokens import TokenStore
from tests.integration.conftest import Live
from tests.stress.support import SCALE

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.stress
SECONDS = 12 * SCALE
CLIENTS = 12
NOTES = 400 * SCALE
RSS_GROWTH_MB = 100 + 20 * SCALE  # a leak shows as a steady climb; a warm cache does not


async def populate(data_dir: Path) -> tuple[Meta, Vocabulary]:
    settings = Settings(data_dir=data_dir)
    meta, vocab = Meta(7), Vocabulary(7)
    meta.users = make_users(NOTES, 7, TokenStore(settings.tokens_path))
    services = await build_services(settings)
    try:
        stats = await ingest(services, meta, NOTES, vocab)
    finally:
        await services.close()
    assert stats["failed"] == 0, stats["error_kinds"]
    return meta, vocab


async def sample_rss(samples: list[float]) -> None:
    while True:
        samples.append(rss_mb(os.getpid()))
        await asyncio.sleep(0.5)


async def test_mixed_load_stays_flat_and_leaves_nothing_behind(tmp_path: Path) -> None:
    meta, vocab = await populate(tmp_path / "data")
    threads_before = {t.ident for t in threading.enumerate()}
    _, fds_before = proc_counts(os.getpid())
    live = Live(tmp_path / "data", rate_limit_mcp=0)
    live.start()
    rss: list[float] = []
    watcher = asyncio.create_task(sample_rss(rss))
    try:
        records = await drive(meta, vocab, live.url, CLIENTS, SECONDS, 2.0, 7)
    finally:
        watcher.cancel()
        live.stop()
    result = summarise(records, SECONDS)
    assert result["errors"] == 0, result["error_kinds"]
    assert result["calls"] > CLIENTS * SECONDS // 2, "the server barely answered"

    drift: dict[str, Any] = result["drift"]["consult"]
    assert drift["last_p50"] <= max(3 * drift["first_p50"], 10.0), drift
    assert drift["last_p95"] <= max(3 * drift["first_p95"], 25.0), drift

    thirds = len(rss) // 3
    assert thirds >= 3, "too few memory samples"
    growth = max(rss[-thirds:]) - max(rss[:thirds])
    assert growth < RSS_GROWTH_MB, f"RSS grew {growth:.0f} MB: {rss[::4]}"

    await asyncio.sleep(0.5)  # let the stopped server's last sockets close
    leaked = [t.name for t in threading.enumerate() if t.ident not in threads_before]
    assert not leaked, f"threads still running after shutdown: {leaked}"
    _, fds_after = proc_counts(os.getpid())
    assert fds_after <= fds_before + 3, f"file descriptors {fds_before} -> {fds_after}"
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not others, f"tasks left in the test loop: {others}"
