"""Copy everything from one store to an empty one, then prove the copy by reading both back.

The source is only ever read (SQLite opened read-only, Postgres in a read-only transaction). Stop
the server first: a write during the copy makes the proof fail, which is the point of the proof.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from hive_mcp.domain.errors import ValidationError
from hive_mcp.ops.migrate.tables import TABLES, Fingerprint, Row, Table

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

log = logging.getLogger("hive.migrate")
BATCH = 500


class Side(Protocol):
    kind: str

    def describe(self) -> str: ...
    def is_empty(self) -> bool: ...
    def count(self, table: str) -> int: ...
    def orgs(self) -> list[str]: ...
    def read(self, table: Table) -> Iterator[Row]: ...
    def blobs(self) -> Iterator[tuple[str, str, bytes]]: ...
    def write(self, table: Table, rows: list[Row]) -> None: ...
    def write_blob(self, org: str, sha: str, data: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class Report:
    copied: dict[str, int] = field(default_factory=dict)
    skipped_expired: int = 0
    verified: bool = False
    problems: list[str] = field(default_factory=list)


def _live(table: Table, rows: Iterator[Row], now: datetime, report: Report) -> Iterator[Row]:
    for row in rows:
        expires = row.get("expires_at") if table.name == "kv" else None
        if expires is not None and expires <= now:
            report.skipped_expired += 1
            continue
        yield row


def _fingerprint(side: Side, table: Table, now: datetime) -> Fingerprint:
    fp = Fingerprint()
    for row in _live(table, side.read(table), now, Report()):
        fp.add(table, row)
    return fp


def _blob_fingerprint(side: Side) -> Fingerprint:
    fp = Fingerprint()
    for org, sha, data in side.blobs():
        fp.add(None, {"org": org, "sha": sha, "size": len(data)})
    return fp


def plan(source: Side, target: Side) -> dict[str, int]:
    """Row counts that would be copied; refuses a target that is not empty."""
    if source.kind == target.kind and source.describe() == target.describe():
        raise ValidationError("the source and the target are the same database")
    if not target.is_empty():
        raise ValidationError(f"{target.describe()} already holds data: migrate into an empty one")
    return {t.name: source.count(t.name) for t in TABLES}


def migrate(source: Side, target: Side, progress: Callable[[str], None] = log.info) -> Report:
    now = datetime.now(UTC)
    report = Report()
    plan(source, target)
    for table in TABLES:
        batch: list[Row] = []
        total = 0
        for row in _live(table, source.read(table), now, report):
            batch.append(row)
            if len(batch) >= BATCH:
                target.write(table, batch)
                total, batch = total + len(batch), []
        if batch:
            target.write(table, batch)
            total += len(batch)
        report.copied[table.name] = total
        progress(f"{table.name}: {total} rows")
    files = 0
    for org, sha, data in source.blobs():
        if sha256(data).hexdigest() != sha:
            raise ValidationError(f"stored file {sha} is corrupted in the source; nothing trusted")
        target.write_blob(org, sha, data)
        files += 1
    report.copied["blobs"] = files
    progress(f"stored files: {files}")
    for table in TABLES:  # the proof: read both back through the same code and compare
        if _fingerprint(source, table, now) != _fingerprint(target, table, now):
            report.problems.append(f"{table.name} differs between source and target")
    if _blob_fingerprint(source) != _blob_fingerprint(target):
        report.problems.append("stored files differ between source and target")
    report.verified = not report.problems
    return report
