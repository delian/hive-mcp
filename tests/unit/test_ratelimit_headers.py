"""Clients can see their allowance: X-RateLimit-* on every limited route, Retry-After on 429."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hive_mcp.ops.ratelimit import RateLimiter, RateLimitMiddleware, Rule


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def client(burst: int, clock: Clock) -> TestClient:
    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    rule = Rule(("/mcp",), RateLimiter(60, burst, clock))
    return TestClient(RateLimitMiddleware(app, [rule]))


def test_allowed_requests_report_the_limit_and_what_is_left():
    c = client(3, Clock())
    seen = [c.post("/mcp").headers for _ in range(3)]
    assert [h["x-ratelimit-limit"] for h in seen] == ["3", "3", "3"]
    assert [h["x-ratelimit-remaining"] for h in seen] == ["2", "1", "0"]
    assert "x-ratelimit-reset" not in seen[0] and "retry-after" not in seen[0]


def test_a_refused_request_says_when_to_come_back():
    c = client(1, Clock())
    assert c.post("/mcp").status_code == 200
    refused = c.post("/mcp")
    assert refused.status_code == 429
    assert refused.headers["x-ratelimit-limit"] == "1"
    assert refused.headers["x-ratelimit-remaining"] == "0"
    assert refused.headers["retry-after"] == refused.headers["x-ratelimit-reset"] == "2"


def test_the_allowance_refills_with_time():
    clock = Clock()
    c = client(2, clock)
    c.post("/mcp"), c.post("/mcp")
    assert c.post("/mcp").status_code == 429
    clock.now += 2  # 60 per minute: one request per second
    ok = c.post("/mcp")
    assert ok.status_code == 200 and ok.headers["x-ratelimit-remaining"] == "1"


def test_routes_without_a_limit_carry_no_headers():
    app = Starlette(routes=[Route("/healthz", lambda r: PlainTextResponse("ok"))])
    rule = Rule(("/mcp",), RateLimiter(60, 1, Clock()))
    response = TestClient(RateLimitMiddleware(app, [rule])).get("/healthz")
    assert not any(k.startswith("x-ratelimit") for k in response.headers)
