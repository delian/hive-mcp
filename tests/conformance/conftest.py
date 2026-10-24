"""Every backend registers a factory here; the whole suite then runs against it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hive_mcp.adapters.memory import MemoryBackend
from hive_mcp.adapters.sqlite import SqliteBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BACKENDS = ["memory", "sqlite"]


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "memory":
        yield MemoryBackend()
        return
    sqlite = SqliteBackend(tmp_path / "data")
    yield sqlite
    sqlite.close()
