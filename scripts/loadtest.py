"""Sustained-load and growth test of a Hive server (see docs/SIZING.md).

    uv run python scripts/loadtest.py --backend sqlite --notes 5000,20000 --clients 24 --seconds 120
    HIVE_TEST_PG_DSN="host=/tmp port=55432 dbname=hive_test user=postgres" \\
        uv run python scripts/loadtest.py --backend postgres --notes 50000

For each corpus size it ingests through the service stack (the product's write path), starts
`hive serve` as a child process, drives a mixed workload over HTTP /mcp, samples memory, disk and
the job backlog, then backs up, verifies and restores. Scratch data is deleted afterwards."""

# ruff: noqa: PLR0913, PLR0917, ANN401

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loadtest_lib import ops, report
from loadtest_lib.population import Meta, Vocabulary, ingest, make_users
from loadtest_lib.sysmon import dir_mb, pg_size_mb, postgres_pss_mb, rss_mb, sampler
from loadtest_lib.workload import drive

from hive_mcp.bootstrap import build_services
from hive_mcp.security.tokens import TokenStore


def parse() -> argparse.Namespace:
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    a.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    a.add_argument("--notes", default="5000", help="corpus size, or a growing list: 5000,20000")
    a.add_argument("--clients", type=int, default=24, help="concurrent simulated agents")
    a.add_argument("--paced-rate", type=float, default=0, help="also run a paced phase (calls/s)")
    a.add_argument("--seconds", type=float, default=120, help="workload length per corpus size")
    a.add_argument("--warmup", type=float, default=5, help="seconds dropped from the statistics")
    a.add_argument("--embedder", choices=("hashing", "onnx"), default="hashing")
    a.add_argument(
        "--vector-store",
        choices=("default", "lancedb"),
        default="default",
        help="sqlite only: keep the vectors in LanceDB (an ANN index)",
    )
    a.add_argument("--models-dir", default=os.environ.get("HIVE_MODELS_DIR"))
    a.add_argument("--pg-dsn", default=os.environ.get("HIVE_TEST_PG_DSN"), help="admin DSN")
    a.add_argument("--workdir", default=None, help="scratch directory (default: a temp dir)")
    a.add_argument("--seed", type=int, default=20260101)
    a.add_argument("--ingest-workers", type=int, default=8)
    a.add_argument("--no-backup", action="store_true", help="skip backup/verify/restore")
    a.add_argument("--reuse", action="store_true", help="sqlite: continue the --workdir data")
    a.add_argument("--idle-seconds", type=float, default=0, help="also run one client alone")
    a.add_argument("--paced-only", action="store_true", help="skip the saturating phase")
    a.add_argument(
        "--backup-during",
        action="store_true",
        help="take an online backup while the paced phase runs",
    )
    a.add_argument("--keep", action="store_true", help="keep the scratch data")
    a.add_argument("--out", default=None, help="write the JSON result here")
    return a.parse_args()


async def ingest_step(settings: Any, meta: Meta, vocab: Vocabulary, target: int, a: Any) -> Any:
    services = await build_services(settings)
    try:

        def progress(i: int, secs: float) -> None:
            print(f"  ingest {i}/{target} {secs:.0f}s rss {rss_mb(os.getpid()):.0f} MB", flush=True)

        stats = await ingest(services, meta, target, vocab, a.ingest_workers, progress)
        stats["ingest_rss_mb"] = round(rss_mb(os.getpid()), 1)
        return stats
    finally:
        await services.close()


async def workload_step(
    server: ops.Server,
    settings: Any,
    meta: Meta,
    vocab: Vocabulary,
    a: Any,
    dsn: str | None,
    rate: float,
    during: Any = None,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    watcher = asyncio.create_task(
        sampler(server.pid, settings.data_dir, server.url, ops.METRICS_TOKEN, samples, dsn)
    )
    side = None
    if during:  # a backup that starts a third of the way into the run, in a thread

        async def later() -> dict[str, Any]:
            await asyncio.sleep(a.warmup + a.seconds / 3)
            return await asyncio.to_thread(during)

        side = asyncio.create_task(later())
    try:
        records = await drive(meta, vocab, server.url, a.clients, a.seconds, a.warmup, a.seed, rate)
    finally:
        watcher.cancel()
    result = report.summarise(records, a.seconds)
    if side:
        result["backup_during"] = await side
    result["memory"] = report.rss_growth(samples)
    jobs = [s["jobs_open"] for s in samples if "jobs_open" in s]
    result["backlog"] = {
        "jobs_open_max": max(jobs, default=0),
        "oldest_due_s_max": max((s.get("oldest_due_s", 0) for s in samples), default=0),
        "vectors_missing_end": next((s["vectors_missing"] for s in reversed(samples)
                                     if "vectors_missing" in s), 0),
        "metrics_scrape_ms_max": max((s.get("scrape_ms", 0) for s in samples), default=0),
    }  # fmt: skip
    result["jobs_by_last"] = next((x["jobs_by"] for x in reversed(samples) if x.get("jobs_by")), {})
    result["threads_fds_end"] = [samples[-1]["threads"], samples[-1]["fds"]] if samples else []
    result["samples"] = samples
    return result


def run_step(target: int, a: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    settings, dsn = ctx["settings"], ctx["dsn"]
    step: dict[str, Any] = {
        "notes": target,
        "backend": a.backend,
        "embedder": a.embedder,
        "vector_store": a.vector_store,
    }
    step["loadavg_start"] = [round(x, 1) for x in os.getloadavg()]
    step["ingest"] = asyncio.run(ingest_step(settings, ctx["meta"], ctx["vocab"], target, a))
    ctx["meta"].save(ctx["meta_path"])
    step["disk_mb"] = round(pg_size_mb(dsn) if dsn else dir_mb(settings.data_dir), 1)
    server = ops.Server(settings, ctx["work"] / "server.log")
    try:
        step["startup_s"] = round(server.wait_ready(), 2)
        phases = [] if a.paced_only else [("workload", 0.0)]
        phases += [("paced", a.paced_rate)] if a.paced_rate else []
        phases += [("idle", 0.0)] if a.idle_seconds else []
        for name, rate in phases:
            during = (lambda: ops.backup_only(settings)) if a.backup_during and rate else None
            cfg = a
            if name == "idle":
                cfg = argparse.Namespace(**{**vars(a), "clients": 1, "seconds": a.idle_seconds})
            step[name] = asyncio.run(
                workload_step(server, settings, ctx["meta"], ctx["vocab"], cfg, dsn, rate, during)
            )
        step["pg_pss_mb"] = round(postgres_pss_mb(), 1) if dsn else None
    finally:
        step["shutdown_s"] = round(server.stop(), 2)
    step["loadavg_end"] = [round(x, 1) for x in os.getloadavg()]
    step["disk_after_mb"] = round(pg_size_mb(dsn) if dsn else dir_mb(settings.data_dir), 1)
    if not a.no_backup:
        step["backup"] = ops.backup_cycle(settings, ctx["restore_settings"])
    return step


def main() -> int:
    a = parse()
    sizes = [int(n) for n in a.notes.split(",")]
    base = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="hive-loadtest-"))
    work = base / "run"
    work.mkdir(parents=True, exist_ok=True)
    data, restored = work / "data", work / "restored"
    data.mkdir(exist_ok=True)
    restored.mkdir(exist_ok=True)
    dsn = restore_dsn = admin = None
    names: list[str] = []
    if a.backend == "postgres":
        if not a.pg_dsn:
            sys.exit("--pg-dsn or HIVE_TEST_PG_DSN is required for --backend postgres")
        admin, stamp = a.pg_dsn, int(time.time())
        names = [f"hive_lt_{stamp}", f"hive_lt_{stamp}_restore"]
        dsn, restore_dsn = (ops.scratch_database(admin, n) for n in names)
    port = ops.free_port()
    models = a.models_dir if a.embedder == "onnx" else None
    settings = ops.settings_for(a.backend, data, dsn, a.embedder, models, port, a.vector_store)
    restore_settings = ops.settings_for(
        a.backend, restored, restore_dsn, a.embedder, models, vector_store=a.vector_store
    )
    meta_path = work / "meta.json"
    if a.reuse and meta_path.exists():
        meta = Meta.load(meta_path)
    else:
        meta = Meta(a.seed)
        meta.users = make_users(max(sizes), a.seed, TokenStore(settings.tokens_path))
    ctx = {"settings": settings, "restore_settings": restore_settings, "dsn": dsn, "work": work,
           "meta": meta, "vocab": Vocabulary(a.seed), "meta_path": meta_path}  # fmt: skip
    steps: list[dict[str, Any]] = []
    try:
        for target in sizes:
            print(f"--- {a.backend}: growing to {target} notes", flush=True)
            steps.append(run_step(target, a, ctx))
            print(report.render(steps[-1]), flush=True)
    finally:
        if admin:
            for n in names:
                ops.drop_database(admin, n)
        if not a.keep and not a.workdir:
            shutil.rmtree(base, ignore_errors=True)
    if a.out:
        Path(a.out).write_text(json.dumps({"args": vars(a), "steps": steps}, indent=1))
    failed = any(s[k]["errors"] for s in steps for k in ("workload", "paced", "idle") if k in s)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
