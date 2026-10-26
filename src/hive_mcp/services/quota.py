"""Per-user storage quotas on new knowledge (items and characters of text).

Usage comes from one aggregate query, cached briefly per user, so a burst of writes costs one query;
the cache is bumped after every accepted write, so a single process cannot overshoot by more than
what other processes add within the cache window. Admins are exempt."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import QuotaExceeded
from hive_mcp.domain.principal import Role

if TYPE_CHECKING:
    from collections.abc import Callable

    from hive_mcp.domain.principal import Principal
    from hive_mcp.ports.maintenance import ItemMaintenance

CACHE_SECONDS = 30.0


class QuotaGuard:
    def __init__(
        self,
        maintenance: ItemMaintenance,
        *,
        max_items: int = 0,
        max_chars: int = 0,
        now: Callable[[], float] = time.monotonic,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._maintenance, self._max_items, self._max_chars = maintenance, max_items, max_chars
        self._now, self._ttl = now, cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, int, int]] = {}  # -> (at, items, chars)

    @property
    def enabled(self) -> bool:
        return bool(self._max_items or self._max_chars)

    async def _usage(self, principal: Principal) -> tuple[int, int]:
        key = (principal.org_id, principal.id)
        cached = self._cache.get(key)
        if cached and self._now() - cached[0] < self._ttl:
            return cached[1], cached[2]
        rows = await self._maintenance.usage(principal.org_id, principal.id)
        items, chars = (rows[0].items, rows[0].chars) if rows else (0, 0)
        self._cache[key] = (self._now(), items, chars)
        return items, chars

    async def check(self, principal: Principal, adding_chars: int) -> None:
        if not self.enabled or principal.role >= Role.ADMIN:
            return
        items, chars = await self._usage(principal)
        if self._max_items and items + 1 > self._max_items:
            raise QuotaExceeded(
                f"item quota reached ({items}/{self._max_items}); "
                "archive or purge items, or ask an admin"
            )
        if self._max_chars and chars + adding_chars > self._max_chars:
            raise QuotaExceeded(
                f"storage quota reached ({chars}/{self._max_chars} characters); "
                "archive or purge items, or ask an admin"
            )

    def record(self, principal: Principal, added_chars: int) -> None:
        key = (principal.org_id, principal.id)
        if key in self._cache:
            at, items, chars = self._cache[key]
            self._cache[key] = (at, items + 1, chars + added_chars)
