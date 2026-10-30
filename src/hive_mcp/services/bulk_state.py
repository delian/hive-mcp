"""Progress records of a bulk folder ingest (stored in the key-value store, one per step)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

NAMESPACE = "bulk_ingest"
RECORD_TTL_SECONDS = 90 * 86400.0
STEP_FILES = 25  # files handled per job step (one lease, one progress record)
MAX_ERRORS = 20  # failures and skip reasons kept per run; the counters keep counting
PIECE_BYTES = 4 * 1024 * 1024  # a huge text file is read and stored in pieces of about this size
BULK_TEXT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # ceiling for plain-text files in background runs
INDEX_RUNS = 50  # runs remembered per organisation for `hive ingest-status`
RUNNING, DONE, CANCELLED, FAILED = "running", "done", "cancelled", "failed"


@dataclass(slots=True)
class Progress:
    """Cumulative state after a step; `cursor` is the last path handled."""

    state: str = RUNNING
    cursor: str | None = None
    done: int = 0
    failed: int = 0
    skipped: int = 0
    bytes: int = 0
    created: int = 0  # items newly stored (0 on a repeat run)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Progress:
        if not data:
            return cls()
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def step_key(run_id: str, step: int) -> str:
    return f"step:{run_id}:{step}"
