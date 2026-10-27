"""Shared helpers: the size knob, seeded randomness and small concurrency utilities."""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

SCALE = max(1, int(os.environ.get("HIVE_STRESS_SCALE", "1")))
SEED = 20260101


def rng(salt: int = 0) -> random.Random:
    """A seeded generator: every run of a test sees the same data."""
    return random.Random(SEED + salt)  # noqa: S311 - test data, not security


async def gather_bounded[T](
    factories: Iterable[Callable[[], Awaitable[T]]], limit: int = 64
) -> list[T | BaseException]:
    """Run the awaitables concurrently (at most `limit` at once); exceptions are returned."""
    sem = asyncio.Semaphore(limit)

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await factory()

    return await asyncio.gather(*(run(f) for f in factories), return_exceptions=True)


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def summarise(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": statistics.median(values), "p95": percentile(values, 95)}


class Stopwatch:
    def __enter__(self) -> Stopwatch:
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self.start
