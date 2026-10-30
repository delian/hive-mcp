"""Small expiring key-value store for OAuth state (clients, codes, tokens, login transactions).

Values are JSON objects. `take` is an atomic get-and-delete, which is what makes single-use records
(authorization codes, login transactions, refresh tokens) safe against concurrent replay.
"""

from __future__ import annotations

from typing import Any, Protocol

JsonObject = dict[str, Any]


class KeyValueStore(Protocol):
    async def put(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> None:
        """Insert or replace. Without a TTL the record never expires."""

    async def put_if_absent(
        self, namespace: str, key: str, value: JsonObject, *, ttl_seconds: float | None = None
    ) -> bool:
        """Insert only if no live record exists. Exactly one concurrent caller gets True."""

    async def get(self, namespace: str, key: str) -> JsonObject | None:
        """Expired records are invisible."""

    async def take(self, namespace: str, key: str) -> JsonObject | None:
        """Atomically return and delete the record. Exactly one concurrent caller gets it."""

    async def delete(self, namespace: str, key: str) -> None: ...

    async def scan(
        self, namespace: str, prefix: str = "", limit: int = 100
    ) -> list[tuple[str, JsonObject]]:
        """Live records whose key starts with `prefix`, in key order, at most `limit`."""

    async def purge_expired(self) -> int:
        """Physically remove expired records (maintenance); returns how many."""
