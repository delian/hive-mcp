"""What every storage backend (SQLite+files, Postgres+files, ...) exposes to the application."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.blobs import BlobStore
    from hive_mcp.ports.indexes import TextIndex, VectorIndex
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.jobs import JobQueue
    from hive_mcp.ports.kv import KeyValueStore


class Backend(Protocol):
    @property
    def items(self) -> ItemRepository: ...
    @property
    def text(self) -> TextIndex: ...
    @property
    def vectors(self) -> VectorIndex: ...
    @property
    def blobs(self) -> BlobStore: ...
    @property
    def jobs(self) -> JobQueue: ...
    @property
    def audit(self) -> AuditLog: ...
    @property
    def kv(self) -> KeyValueStore: ...

    def close(self) -> object:
        """Sync backends return None, async ones an awaitable; `close_backend` handles both."""


async def close_backend(backend: Backend) -> None:
    result = backend.close()
    if inspect.isawaitable(result):
        await result
