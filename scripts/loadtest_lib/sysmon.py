"""Process and storage sampling from /proc, the data directory and the server's /metrics."""

# ruff: noqa: PLR0913, PLR0917

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path

METRIC = re.compile(
    r"^(hive_jobs|hive_job_oldest_due_seconds|hive_vector_items_missing)\{?(.*?)\}? ([\d.e+-]+)$"
)


def rss_mb(pid: int) -> float:
    try:
        for line in open(f"/proc/{pid}/status"):  # noqa: SIM115
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def proc_counts(pid: int) -> tuple[int, int]:
    """(threads, open file descriptors) of a process."""
    try:
        threads = len(os.listdir(f"/proc/{pid}/task"))
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0, 0
    return threads, fds


def postgres_pss_mb() -> float:
    """Proportional memory of every postgres process (shared buffers are split fairly)."""
    total = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if open(f"/proc/{name}/comm").read().strip() != "postgres":  # noqa: SIM115
                continue
            for line in open(f"/proc/{name}/smaps_rollup"):  # noqa: SIM115
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
        except OSError:
            continue
    return total / 1024


def dir_mb(path: Path) -> float:
    """Total size of a directory tree (SQLite file, WAL, blobs, state files)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total / 1048576


def pg_size_mb(dsn: str) -> float:
    import psycopg  # noqa: PLC0415 - postgres extra

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    return float(row[0]) / 1048576 if row else 0.0


async def scrape(http: httpx.AsyncClient, url: str, token: str) -> dict[str, Any]:
    """Job backlog, oldest due job age and vector coverage gap; also how long the scrape took."""
    started = time.perf_counter()
    out: dict[str, Any] = {"jobs_open": 0, "oldest_due_s": 0.0, "vectors_missing": 0}
    resp = await http.get(f"{url}/metrics", headers={"Authorization": f"Bearer {token}"})
    out["scrape_ms"] = round((time.perf_counter() - started) * 1000, 1)
    for line in resp.text.splitlines():
        m = METRIC.match(line)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(2), float(m.group(3))
        if name == "hive_jobs" and 'state="done"' not in labels:
            out["jobs_open"] += int(value)
            out.setdefault("jobs_by", {})[labels.replace('"', "")] = int(value)
        elif name == "hive_job_oldest_due_seconds":
            out["oldest_due_s"] = max(out["oldest_due_s"], value)
        elif name == "hive_vector_items_missing":
            out["vectors_missing"] = int(value)
    return out


async def sampler(
    pid: int,
    data_dir: Path,
    url: str,
    token: str,
    samples: list[dict[str, Any]],
    pg_dsn: str | None = None,
    every: float = 2.0,
    scrape_every: int = 5,
) -> None:
    """Appends one dict per tick until cancelled; the first tick is time zero."""
    t0, tick = time.monotonic(), 0
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            threads, fds = proc_counts(pid)
            row: dict[str, Any] = {
                "t": round(time.monotonic() - t0, 1), "rss_mb": round(rss_mb(pid), 1),
                "threads": threads, "fds": fds,
                "disk_mb": round(pg_size_mb(pg_dsn) if pg_dsn else dir_mb(data_dir), 1),
            }  # fmt: skip
            if pg_dsn:
                row["pg_pss_mb"] = round(postgres_pss_mb(), 1)
            if tick % scrape_every == 0:
                with contextlib.suppress(httpx.HTTPError, OSError):
                    row.update(await scrape(http, url, token))
            samples.append(row)
            tick += 1
            await asyncio.sleep(every)
