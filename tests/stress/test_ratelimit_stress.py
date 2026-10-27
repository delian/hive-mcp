"""Token buckets under many concurrent callers: never more than burst + rate x time, nothing is
starved, keys are isolated, and the Postgres-shared limiter holds across 'replicas'."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hive_mcp.ops.ratelimit import MAX_KEYS, RateLimiter, RateLimitMiddleware, Rule
from tests.stress.support import SCALE, rng

pytestmark = pytest.mark.stress


class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


async def test_a_simultaneous_crowd_gets_exactly_the_burst() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=120, burst=25, now=clock)  # 2 tokens per second
    callers = 2000 * SCALE
    waits = await asyncio.gather(*(limiter.ahit("k") for _ in range(callers)))
    assert sum(w == 0.0 for w in waits) == 25
    assert all(0 < w <= 0.5 + 1e-9 for w in waits if w)  # told how long until a token exists


async def test_the_total_allowed_never_exceeds_burst_plus_rate_times_time_and_never_starves() -> (
    None
):
    gen = rng(11)
    clock = Clock()
    rate_per_s, burst = 3.0, 20
    limiter = RateLimiter(per_minute=rate_per_s * 60, burst=burst, now=clock)
    start, allowed, elapsed = clock.t, 0, 0.0
    for _tick in range(300 * SCALE):
        clock.t += gen.choice([0.0, 0.0, 0.01, 0.1, 0.4, 1.0, 2.5])
        elapsed = clock.t - start
        crowd = gen.randrange(1, 40)
        waits = await asyncio.gather(*(limiter.ahit("k") for _ in range(crowd)))
        allowed += sum(w == 0.0 for w in waits)
        ceiling = burst + rate_per_s * elapsed
        assert allowed <= ceiling + 1e-6, f"allowed {allowed} > {ceiling:.2f} at t={elapsed:.2f}"
    assert allowed > burst  # the refill really happened


async def test_a_crowd_always_at_the_door_gets_exactly_what_was_refilled() -> None:
    clock = Clock()
    rate_per_s, burst = 4.0, 16
    limiter = RateLimiter(per_minute=rate_per_s * 60, burst=burst, now=clock)
    gen = rng(12)
    start = clock.t  # a bucket is born full when first seen
    waits = await asyncio.gather(*(limiter.ahit("k") for _ in range(burst + 5)))
    allowed = sum(w == 0.0 for w in waits)
    assert allowed == burst
    for _tick in range(200 * SCALE):
        clock.t += gen.choice([0.0, 0.125, 0.25, 0.5, 1.0])  # exact in binary floating point
        waits = await asyncio.gather(*(limiter.ahit("k") for _ in range(burst + 5)))
        allowed += sum(w == 0.0 for w in waits)
        # tokens are never wasted: refills (capped only at `burst`) are fully used each tick
        # because the crowd is bigger than the bucket
        assert allowed == int(burst + rate_per_s * (clock.t - start))


async def test_waiting_as_told_always_succeeds_next() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=3, now=clock)
    for _ in range(3):
        assert await limiter.ahit("k") == 0.0
    for _ in range(50 * SCALE):
        wait = await limiter.ahit("k")
        assert wait > 0
        clock.t += wait
        assert await limiter.ahit("k") == 0.0


async def test_keys_do_not_affect_each_other() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=4, now=clock)
    keys = [f"user{i}" for i in range(50 * SCALE)]
    hammered = await asyncio.gather(*(limiter.ahit("hog") for _ in range(500)))
    assert sum(w == 0.0 for w in hammered) == 4
    got = await asyncio.gather(*(limiter.ahit(k) for k in keys for _ in range(6)))
    for i, _key in enumerate(keys):
        assert sum(w == 0.0 for w in got[i * 6 : (i + 1) * 6]) == 4  # each its own burst of 4
    assert await limiter.ahit("hog") > 0


async def test_real_clock_crowd_respects_the_ceiling() -> None:
    limiter = RateLimiter(per_minute=600, burst=10)  # 10 per second
    start = time.monotonic()
    allowed = 0
    for _ in range(20):
        waits = await asyncio.gather(*(limiter.ahit("k") for _ in range(100)))
        allowed += sum(w == 0.0 for w in waits)
        await asyncio.sleep(0.01)
    elapsed = time.monotonic() - start
    assert 10 <= allowed <= 10 + 10 * elapsed + 1e-6, (allowed, elapsed)


async def test_memory_stays_bounded_with_a_flood_of_keys_and_keeps_the_offender() -> None:
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=1, now=clock)
    await limiter.ahit("offender")
    assert await limiter.ahit("offender") > 0
    for i in range(MAX_KEYS + MAX_KEYS // 2):
        clock.t += 1e-6  # time passes (a millisecond per 1000 callers), refilling almost nothing
        await limiter.ahit(f"drive-by{i}")
        if i % 1000 == 0:
            assert await limiter.ahit("offender") > 0, "the offender was forgotten"
    assert len(limiter._buckets) <= MAX_KEYS + 1


def app_with(*rules: Rule) -> RateLimitMiddleware:
    async def ok(request):
        return PlainTextResponse("ok")

    return RateLimitMiddleware(
        Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])]), list(rules)
    )


async def test_middleware_answers_a_crowd_with_429_and_retry_after_per_token() -> None:
    clock = Clock()
    app = app_with(Rule(("/mcp",), RateLimiter(60, 8, now=clock), by_token=True))
    tokens = [f"Bearer tok{i}" for i in range(6)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        responses = await asyncio.gather(
            *(
                c.get("/mcp", headers={"Authorization": t})
                for t in tokens
                for _ in range(40 * SCALE)
            )
        )
    for i in range(6):
        group = responses[i * 40 * SCALE : (i + 1) * 40 * SCALE]
        assert sum(r.status_code == 200 for r in group) == 8
        limited = [r for r in group if r.status_code == 429]
        assert len(limited) == len(group) - 8
        for r in limited:
            assert int(r.headers["retry-after"]) >= 1 and r.json()["error"] == "rate_limited"


# ------------------------------------------------------------------------- shared (Postgres)
@pytest.fixture
def pg(backend):
    if type(backend).__name__ != "PostgresBackend":
        pytest.skip("the shared limiter needs the Postgres backend")
    return backend


async def test_postgres_limiter_holds_across_replicas(pg) -> None:
    from hive_mcp.adapters.postgres.ratelimit import PgRateLimiter

    replicas = [PgRateLimiter(lambda: pg.db, per_minute=120, burst=12) for _ in range(3)]
    start = time.monotonic()
    callers = 60 * SCALE
    waits = await asyncio.gather(*(replicas[i % 3].ahit("shared") for i in range(callers)))
    elapsed = time.monotonic() - start
    allowed = sum(w == 0.0 for w in waits)
    assert 12 <= allowed <= 12 + 2.0 * elapsed + 1e-6, (allowed, elapsed)
    assert all(w > 0 for w in waits if w != 0.0)
    other = await asyncio.gather(*(replicas[0].ahit(f"other{i}") for i in range(10)))
    assert other == [0.0] * 10  # another key is untouched by the crowd


async def test_postgres_limiter_never_exceeds_the_ceiling_over_time(pg) -> None:
    from hive_mcp.adapters.postgres.ratelimit import PgRateLimiter

    limiter_a = PgRateLimiter(lambda: pg.db, per_minute=300, burst=5)  # 5 per second
    limiter_b = PgRateLimiter(lambda: pg.db, per_minute=300, burst=5)
    start, allowed = time.monotonic(), 0
    for _ in range(8):
        waits = await asyncio.gather(
            *(lim.ahit("k") for lim in (limiter_a, limiter_b) for _ in range(10))
        )
        allowed += sum(w == 0.0 for w in waits)
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - start
    assert 5 <= allowed <= 5 + 5 * elapsed + 1e-6, (allowed, elapsed)
