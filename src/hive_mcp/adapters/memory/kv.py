"""In-memory KeyValueStore (reference implementation for the conformance suite)."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.kv import JsonObject


class MemoryKv:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._rows: dict[tuple[str, str], tuple[JsonObject, datetime | None]] = {}

    def _live(self, namespace: str, key: str) -> JsonObject | None:
        row = self._rows.get((namespace, key))
        if row is None:
            return None
        value, expires = row
        if expires is not None and expires <= self._clock.now():
            del self._rows[(namespace, key)]
            return None
        return value

    def _expiry(self, ttl_seconds: float | None) -> datetime | None:
        return None if ttl_seconds is None else self._clock.now() + timedelta(seconds=ttl_seconds)

    async def put(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> None:
        self._rows[(namespace, key)] = (copy.deepcopy(value), self._expiry(ttl_seconds))

    async def put_if_absent(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> bool:
        if self._live(namespace, key) is not None:
            return False
        await self.put(namespace, key, value, ttl_seconds=ttl_seconds)
        return True

    async def get(self, namespace: str, key: str) -> JsonObject | None:
        value = self._live(namespace, key)
        return copy.deepcopy(value) if value is not None else None

    async def take(self, namespace: str, key: str) -> JsonObject | None:
        value = self._live(namespace, key)
        self._rows.pop((namespace, key), None)
        return copy.deepcopy(value) if value is not None else None

    async def delete(self, namespace: str, key: str) -> None:
        self._rows.pop((namespace, key), None)

    async def purge_expired(self) -> int:
        now = self._clock.now()
        dead = [k for k, (_, exp) in self._rows.items() if exp is not None and exp <= now]
        for k in dead:
            del self._rows[k]
        return len(dead)
