import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hive_mcp.ops.ratelimit import MAX_KEYS, RateLimiter, RateLimitMiddleware, Rule


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_burst_then_refill():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=3, now=clock)  # 1 token per second
    assert [limiter.hit("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.hit("a")
    assert 0.9 < wait <= 1.0  # told how long until the next token
    clock.t += 1.0
    assert limiter.hit("a") == 0.0 and limiter.hit("a") > 0
    clock.t += 3600
    assert [limiter.hit("a") for _ in range(3)] == [0.0] * 3 and limiter.hit(
        "a"
    ) > 0  # capped at burst


def test_keys_are_independent():
    limiter = RateLimiter(60, 1, now=Clock())
    assert limiter.hit("a") == 0.0 and limiter.hit("b") == 0.0 and limiter.hit("a") > 0


def test_memory_stays_bounded_without_forgetting_active_offenders():
    clock = Clock()
    limiter = RateLimiter(60, 1, now=clock)
    limiter.hit("offender")
    assert limiter.hit("offender") > 0
    for i in range(MAX_KEYS + 10):
        limiter.hit(f"k{i}")
    assert len(limiter._buckets) <= MAX_KEYS + 10
    clock.t += 1000  # everything refilled: trimming may now drop all of it
    limiter.hit("x")
    for i in range(MAX_KEYS + 1):
        limiter.hit(f"j{i}")
    assert len(limiter._buckets) <= MAX_KEYS


def app_with(*rules):
    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/a", ok, methods=["GET", "POST"]), Route("/free", ok)])
    return RateLimitMiddleware(inner, list(rules))


async def get(app, path, headers=None, method="GET"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, path, headers=headers)


async def test_middleware_returns_429_with_retry_after_only_on_matching_routes():
    app = app_with(Rule(("/a",), RateLimiter(60, 2, now=Clock())))
    assert [(await get(app, "/a")).status_code for _ in range(3)] == [200, 200, 429]
    blocked = await get(app, "/a")
    assert blocked.headers["retry-after"] and blocked.json()["error"] == "rate_limited"
    assert (await get(app, "/free")).status_code == 200  # unrelated routes are never limited


async def test_method_filter_and_per_token_keys():
    reg = app_with(Rule(("/a",), RateLimiter(60, 1, now=Clock()), methods=frozenset({"POST"})))
    assert (await get(reg, "/a", method="GET")).status_code == 200
    assert (await get(reg, "/a", method="GET")).status_code == 200  # GET not limited
    assert (await get(reg, "/a", method="POST")).status_code == 200
    assert (await get(reg, "/a", method="POST")).status_code == 429
    tok = app_with(Rule(("/a",), RateLimiter(60, 1, now=Clock()), by_token=True))
    one, two = {"Authorization": "Bearer one"}, {"Authorization": "Bearer two"}
    assert (await get(tok, "/a", one)).status_code == 200
    assert (await get(tok, "/a", one)).status_code == 429
    assert (await get(tok, "/a", two)).status_code == 200  # another token has its own bucket
    assert (await get(tok, "/a")).status_code == 200  # anonymous callers are keyed by address
    assert (await get(tok, "/a")).status_code == 429


async def test_concurrent_requests_cannot_exceed_the_burst():
    app = app_with(Rule(("/a",), RateLimiter(60, 5, now=Clock())))
    codes = await asyncio.gather(*(get(app, "/a") for _ in range(20)))
    assert sum(r.status_code == 200 for r in codes) == 5
