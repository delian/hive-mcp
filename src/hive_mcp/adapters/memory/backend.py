"""Bundle of in-memory stores; the shape every backend factory returns to services and tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from hive_mcp.adapters.memory.audit import MemoryAudit
from hive_mcp.adapters.memory.blobs import MemoryBlobs
from hive_mcp.adapters.memory.items import MemoryItems
from hive_mcp.adapters.memory.jobs import MemoryJobs
from hive_mcp.adapters.memory.kv import MemoryKv
from hive_mcp.adapters.memory.text import MemoryText
from hive_mcp.adapters.memory.vectors import MemoryVectors
from hive_mcp.domain.clock import FixedClock


@dataclass
class MemoryBackend:
    clock: FixedClock = field(default_factory=FixedClock)
    items: MemoryItems = field(init=False)
    text: MemoryText = field(default_factory=MemoryText)
    vectors: MemoryVectors = field(default_factory=MemoryVectors)
    blobs: MemoryBlobs = field(default_factory=MemoryBlobs)
    jobs: MemoryJobs = field(init=False)
    audit: MemoryAudit = field(default_factory=MemoryAudit)
    kv: MemoryKv = field(init=False)

    def __post_init__(self) -> None:
        self.items = MemoryItems(self.clock)
        self.jobs = MemoryJobs(self.clock)
        self.kv = MemoryKv(self.clock)
