"""Token-bucket limiter shared by all replicas through one table. A database failure lets the request
through: rate limiting protects the service, it must never be the reason the service is down."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import HiveError

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.adapters.postgres.db import PgDatabase

log = logging.getLogger("hive.ratelimit")
NOW = "clock_timestamp()"


class PgRateLimiter:
    def __init__(self, get_db: Callable[[], PgDatabase], per_minute: float, burst: int) -> None:
        self._get_db, self._rate, self._burst = get_db, per_minute / 60.0, float(burst)

    async def ahit(self, key: str) -> float:
        """0.0 if allowed, else seconds to wait (same contract as RateLimiter.hit)."""
        try:
            return await self._hit(key)
        except HiveError:
            log.warning("ratelimit.store_unavailable")
            return 0.0

    async def _hit(self, key: str) -> float:
        async with self._get_db().tx() as conn:
            await conn.execute(
                f"INSERT INTO rate_buckets (key, tokens, seen) VALUES (%s, %s, {NOW}) "  # noqa: S608
                "ON CONFLICT DO NOTHING",
                (key, self._burst),
            )
            cur = await conn.execute(
                f"SELECT tokens, extract(epoch FROM {NOW} - seen) AS age "  # noqa: S608
                "FROM rate_buckets WHERE key = %s FOR UPDATE",
                (key,),
            )
            row = await cur.fetchone()
            if row is None:  # cannot happen: the row was inserted in this transaction
                return 0.0
            tokens = min(
                self._burst, float(row["tokens"]) + max(float(row["age"]), 0.0) * self._rate
            )
            allowed = tokens >= 1.0
            await conn.execute(
                f"UPDATE rate_buckets SET tokens = %s, seen = {NOW} WHERE key = %s",  # noqa: S608
                (tokens - 1.0 if allowed else tokens, key),
            )
        if allowed:
            return 0.0
        return (1.0 - tokens) / self._rate if self._rate > 0 else 3600.0
