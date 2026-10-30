"""Latency statistics, thirds comparison and the printed summary."""

# ruff: noqa: PLR2004

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


def stats_of(values: list[float]) -> dict[str, float]:
    ms = [v * 1000 for v in values]
    return {
        "n": len(ms), "p50": round(pct(ms, 50), 1), "p95": round(pct(ms, 95), 1),
        "p99": round(pct(ms, 99), 1), "max": round(max(ms, default=0), 1),
        "mean": round(statistics.fmean(ms), 1) if ms else 0.0,
    }  # fmt: skip


def summarise(records: list[tuple[str, float, float, str]], seconds: float) -> dict[str, Any]:
    """records: (op, start offset s, duration s, outcome); outcome 'ok', 'conflict' or an error."""
    by_op: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    third = max(seconds / 3, 1e-9)
    thirds: dict[str, list[list[float]]] = defaultdict(lambda: [[], [], []])
    for op, start, dur, outcome in records:
        if outcome not in ("ok", "conflict"):
            errors[f"{op}: {outcome}"] += 1
            continue
        by_op[op].append(dur)
        thirds[op][min(int(start / third), 2)].append(dur)
    ops = {op: stats_of(v) for op, v in sorted(by_op.items())}
    drift = {
        op: {"first_p50": stats_of(t[0])["p50"], "last_p50": stats_of(t[2])["p50"],
             "first_p95": stats_of(t[0])["p95"], "last_p95": stats_of(t[2])["p95"]}
        for op, t in thirds.items()
    }  # fmt: skip
    total = len(records)
    return {
        "calls": total, "errors": sum(errors.values()), "error_kinds": dict(errors),
        "calls_per_s": round(total / seconds, 1) if seconds else 0, "ops": ops, "drift": drift,
    }  # fmt: skip


def rss_growth(samples: list[dict[str, Any]]) -> dict[str, float]:
    rss = [s["rss_mb"] for s in samples if s.get("rss_mb")]
    if len(rss) < 4:
        return {}
    k = max(len(rss) // 5, 1)
    return {
        "rss_start_mb": round(statistics.fmean(rss[:k]), 1),
        "rss_end_mb": round(statistics.fmean(rss[-k:]), 1),
        "rss_peak_mb": round(max(rss), 1),
    }


def render(step: dict[str, Any]) -> str:
    """A compact, readable block for one corpus size."""
    lines = [f"== {step['notes']} notes on {step['backend']} =="]
    ing = step.get("ingest", {})
    if ing:
        lines.append(
            f"ingest: {ing['added']} notes in {ing['seconds']}s = {ing['notes_per_s']}/s "
            f"(remember p50 {ing['remember_p50_ms']} ms, p95 {ing['remember_p95_ms']} ms, "
            f"failed {ing['failed']})"
        )
    for kind, n in ing.get("error_kinds", {}).items():
        lines.append(f"  INGEST ERROR x{n}: {kind}")
    for key in ("startup_s", "shutdown_s", "disk_mb", "backup", "restore"):
        if key in step:
            lines.append(f"{key}: {step[key]}")
    for name in ("workload", "paced", "idle"):
        w = step.get(name)
        if not w:
            continue
        lines.append(f"{name}: {w['calls']} calls, {w['calls_per_s']}/s, errors {w['errors']}")
        for op, s in w["ops"].items():
            lines.append(
                f"  {op:<10} n={s['n']:<6} p50 {s['p50']:>7} p95 {s['p95']:>7} "
                f"p99 {s['p99']:>7} max {s['max']:>8} ms"
            )
        for kind, n in w["error_kinds"].items():
            lines.append(f"  ERROR x{n}: {kind}")
        if "backup_during" in w:
            lines.append(f"online backup during this phase: {w['backup_during']}")
        lines.append(f"memory: {w.get('memory')}")
        if w.get("backlog"):
            lines.append(f"backlog: {w['backlog']}")
    return "\n".join(lines)
