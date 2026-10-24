"""Every backend registers a factory here; the whole suite then runs against it."""

from __future__ import annotations

import pytest

from hive_mcp.adapters.memory import MemoryBackend

BACKENDS = {"memory": MemoryBackend}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: pytest.FixtureRequest) -> MemoryBackend:
    return BACKENDS[request.param]()
