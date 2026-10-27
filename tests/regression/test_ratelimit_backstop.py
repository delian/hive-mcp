"""BUG-OPS-13: rotating made-up bearer tokens gave every request a fresh bucket (unlimited flood)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hive_mcp.app import BACKSTOP_FACTOR, rate_limit_rules
from hive_mcp.config import Settings
from hive_mcp.ops.ratelimit import RateLimitMiddleware


def client(**settings) -> TestClient:
    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    rules = rate_limit_rules(Settings(rate_limit_mcp=60, **settings))
    return TestClient(RateLimitMiddleware(app, rules))


def test_rotating_bogus_bearers_from_one_address_is_stopped():
    c = client()
    limit = 10 * BACKSTOP_FACTOR  # burst of 10 per token, times the backstop factor
    codes = [
        c.post("/mcp", headers={"Authorization": f"Bearer bogus-{n}"}).status_code
        for n in range(limit + 50)
    ]
    assert 429 in codes and codes.count(200) <= limit * 1.5  # a little refills while it runs
    assert codes.index(429) > 10  # not so tight that a few different tokens are refused


def test_one_token_is_still_limited_by_its_own_bucket():
    c = client()
    codes = [c.post("/mcp", headers={"Authorization": "Bearer one"}).status_code for _ in range(30)]
    assert codes.count(200) <= 10 + 1 and 429 in codes
