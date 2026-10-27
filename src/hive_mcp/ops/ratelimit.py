"""Per-key request limits (token buckets) as ASGI middleware.

RateLimiter is in-process (one bucket set per replica); with HIVE_REPLICAS > 1 the Postgres-backed
limiter (adapters/postgres/ratelimit.py) is used instead so the limit holds across replicas.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

MAX_KEYS = 50_000


class Limiter(Protocol):
    async def ahit(self, key: str) -> float: ...


class RateLimiter:
    """Allows `burst` requests at once, refilling `per_minute` per minute."""

    def __init__(
        self, per_minute: float, burst: int, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate, self._burst, self._now = per_minute / 60.0, float(burst), now
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last seen)

    def hit(self, key: str) -> float:
        """0.0 if allowed, else the seconds to wait before the next request would be."""
        now = self._now()
        tokens, seen = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - seen) * self._rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            self._trim(now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (1.0 - tokens) / self._rate if self._rate > 0 else 3600.0

    async def ahit(self, key: str) -> float:
        return self.hit(key)

    def _trim(self, now: float) -> None:
        if len(self._buckets) <= MAX_KEYS:
            return
        # Forget keys that have fully refilled (they act as new anyway); else the least recent.
        full = [
            k for k, (t, s) in self._buckets.items() if t + (now - s) * self._rate >= self._burst
        ]
        for key in (
            full or sorted(self._buckets, key=lambda k: self._buckets[k][1])[: MAX_KEYS // 10]
        ):
            del self._buckets[key]


@dataclass(frozen=True, slots=True)
class Rule:
    prefixes: tuple[str, ...]
    limiter: Limiter
    by_token: bool = False  # key on the bearer token (hashed) when present, else on the address
    methods: frozenset[str] = frozenset()  # empty = all
    # a different limiter for particular keys (e.g. a token with its own limit); None = this rule's
    per_key: Callable[[str], Limiter | None] | None = None
    # By-token rules key on whatever bearer string arrives, before it is known to be valid, so
    # rotating made-up tokens would give every request a fresh bucket. The backstop also counts
    # every request of the same address (generously: many people may share one address).
    backstop: Limiter | None = None


def client_key(scope: Scope) -> str:
    client = scope.get("client")
    return f"ip:{client[0]}" if client else "ip:unknown"


def token_key(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization" and value[:7].lower() == b"bearer ":
            return "tok:" + hashlib.sha256(value[7:]).hexdigest()[:32]
    return None


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, rules: list[Rule]) -> None:
        self.app, self.rules = app, rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path, method = scope["path"], scope["method"]
            for rule in self.rules:
                if path.startswith(rule.prefixes) and (not rule.methods or method in rule.methods):
                    key = (token_key(scope) if rule.by_token else None) or client_key(scope)
                    limiter = (rule.per_key and rule.per_key(key)) or rule.limiter
                    wait = await limiter.ahit(f"{rule.prefixes[0]}|{key}")
                    if wait <= 0 and rule.backstop is not None and key.startswith("tok:"):
                        wait = await rule.backstop.ahit(f"{rule.prefixes[0]}|{client_key(scope)}")
                    if wait > 0:
                        response = JSONResponse(
                            {"error": "rate_limited", "error_description": "slow down and retry"},
                            status_code=429,
                            headers={"Retry-After": str(int(wait) + 1)},
                        )
                        await response(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)
