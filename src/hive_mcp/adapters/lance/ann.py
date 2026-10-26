"""The approximate (IVF) index of a Lance table: when to build it, and how hard to search it.

Measured on 220k real embeddings: probing most partitions is needed because the access filter
leaves few vectors in each one (see docs/STORAGE.md)."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Any

from hive_mcp.security.jsonfile import AtomicJsonFile

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)
INDEX_INFO_SECONDS = 30.0  # how long a table's index size is remembered
# Approximate search (measured on 220k real embeddings with a 0.3%-visible access filter: exact
# 345 ms, IVF_FLAT with sqrt(n) partitions and 85% of them probed 26 ms at recall@10 0.995; probing
# 43% gave 0.96, 21% gave 0.72, because the filter leaves few vectors in each partition).
INDEX_MIN_ROWS = 50_000  # below this an exact scan is fast enough
NPROBE_FRACTION = 0.85
REBUILD_FACTOR = 2.0  # rebuild the index when the table has doubled since it was built
INDEX_NAME = "vector_idx"


class AnnIndex:
    def __init__(
        self,
        db: Any,  # noqa: ANN401 - lancedb connection
        directory: Path,
        *,
        min_rows: int = INDEX_MIN_ROWS,
        nprobe_fraction: float = NPROBE_FRACTION,
    ) -> None:
        self._db, self._min_rows, self._fraction = db, min_rows, nprobe_fraction
        self.nprobes: int | None = None  # overrides the computed number of partitions to probe
        self.building: set[str] = set()
        self._info: dict[str, tuple[float, int]] = {}  # table -> (checked at, rows at build)
        self._state = AtomicJsonFile(directory / "index_state.json", default={})

    # Writes keep the index current (optimize() indexes new rows), but its partitions were chosen
    # for the table's size at build time, so quality fades as the table grows: remember that size.
    def _built_rows(self, name: str) -> int:
        return int(self._state.read().get(name, 0))

    def _remember_build(self, name: str, rows: int) -> None:
        state = self._state.read()
        state[name] = rows
        self._state.write(state)

    @staticmethod
    def has_index(table: Any) -> bool:  # noqa: ANN401
        try:
            return int(table.index_stats(INDEX_NAME).num_indexed_rows) > 0
        except Exception:
            return False

    def maybe_build(self, table: Any) -> None:  # noqa: ANN401
        """Build or rebuild the IVF index in the background once the table is big enough. Searches
        keep working meanwhile (rows not yet indexed are scanned exactly), so a failed or
        conflicting build is simply tried again at the next compaction."""
        name = table.name
        rows = table.count_rows()
        if rows < self._min_rows or name in self.building:
            return
        if self.has_index(table) and rows < REBUILD_FACTOR * self._built_rows(name):
            return
        self.building.add(name)
        threading.Thread(target=self._build_index, args=(name, rows), daemon=True).start()

    def _build_index(self, name: str, rows: int) -> None:
        from lancedb.index import IvfFlat  # noqa: PLC0415

        try:
            table = self._db.open_table(name)
            config = IvfFlat(distance_type="cosine", num_partitions=max(int(rows**0.5), 8))
            table.create_index("vector", config=config, name=INDEX_NAME, replace=True)
            self._remember_build(name, rows)
            self._info.pop(name, None)
            log.info("lance.index_built table=%s rows=%d", name, rows)
        except Exception:
            log.exception("lance.index_failed table=%s (searches stay exact; retried later)", name)
        finally:
            self.building.discard(name)

    def probes(self, table: Any) -> int | None:  # noqa: ANN401
        """How many partitions to search: all but a sliver, because the access filter leaves few
        vectors per partition. None while there is no index (exact search)."""
        if self.nprobes:
            return self.nprobes
        checked, built = self._info.get(table.name, (0.0, 0))
        if time.monotonic() - checked > INDEX_INFO_SECONDS:
            built = self._built_rows(table.name) if self.has_index(table) else 0
            self._info[table.name] = (time.monotonic(), built)
        return math.ceil(self._fraction * max(int(built**0.5), 8)) if built else None
