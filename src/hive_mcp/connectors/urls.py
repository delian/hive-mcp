"""Connector for a fixed list of web pages and documents (an intranet wiki, a handbook)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hive_mcp.connectors.base import Document
from hive_mcp.ingest.fetch import fetch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from hive_mcp.ingest.fetch import FetchPolicy


class UrlConnector:
    def __init__(self, urls: list[str], policy: FetchPolicy) -> None:
        self._urls, self._policy = urls, policy

    async def documents(self) -> AsyncIterator[Document]:
        for url in self._urls:
            yield Document(url, url, self._loader(url))

    def _loader(self, url: str) -> Callable[[], Awaitable[tuple[str, bytes]]]:
        async def load() -> tuple[str, bytes]:
            fetched = await fetch(url, self._policy)
            return fetched.name, fetched.data

        return load
