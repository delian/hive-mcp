"""Write an organisation's items and links to a directory, page by page.

Private notes are left out unless the operator asks; archived ones likewise. The text is exported
as stored (already secret-masked). The directory appears atomically: everything is written to a
private staging directory next to it and renamed when complete."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.item import Item, Status
from hive_mcp.domain.scope import ScopeLevel
from hive_mcp.export.model import (
    EDGE_COLUMNS,
    ITEM_COLUMNS,
    SCHEMA_VERSION,
    Pseudonymiser,
    edge_row,
    item_row,
)
from hive_mcp.export.writers import RowWriter, check_format, make_writer, sha256_file
from hive_mcp.ports.audit import AuditEvent
from hive_mcp.ports.maintenance import ItemFilter

if TYPE_CHECKING:
    from datetime import datetime

    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.audit import AuditLog
    from hive_mcp.ports.edges import EdgeRepository
    from hive_mcp.ports.items import ItemRepository
    from hive_mcp.ports.maintenance import ItemMaintenance

ACTION = "export.run"
ITEM_PAGE = 200
EDGE_PAGE = 500


@dataclass(frozen=True, slots=True)
class ExportOptions:
    format: str = "jsonl"
    include_private: bool = False
    include_archived: bool = False
    since: datetime | None = None
    pseudonymise: bool = False


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    items: int
    edges: int
    manifest: dict[str, Any]


def _exportable(item: Item | None, opts: ExportOptions) -> bool:
    return (
        item is not None
        and (opts.include_private or item.scope.level is not ScopeLevel.PRIVATE)
        and (opts.include_archived or item.status is not Status.ARCHIVED)
    )


class _Pass:
    """One export run: the ports it reads and the request it serves."""

    def __init__(
        self, items: ItemRepository, maintenance: ItemMaintenance, edges: EdgeRepository
    ) -> None:
        self.items, self.maintenance, self.edges = items, maintenance, edges

    async def write_items(
        self, org_id: str, opts: ExportOptions, alias: Pseudonymiser, writer: RowWriter
    ) -> int:
        flt = ItemFilter(
            org_id=org_id,
            exclude_private=not opts.include_private,
            exclude_archived=not opts.include_archived,
            updated_since=opts.since,
        )
        count, after = 0, None
        while page := await self.maintenance.find_system(flt, after_id=after, limit=ITEM_PAGE):
            writer.write([item_row(i, alias) for i in page])
            count, after = count + len(page), page[-1].id
        return count

    async def write_edges(
        self, org_id: str, opts: ExportOptions, alias: Pseudonymiser, writer: RowWriter
    ) -> int:
        """Links whose both ends are exportable (so the id of a left-out note never leaks)."""
        count, after = 0, None
        while page := await self.edges.list_page(org_id, after=after, limit=EDGE_PAGE):
            after = page[-1].key
            seen: dict[str, bool] = {}
            keep = []
            for e in page:
                if opts.since is not None and e.created_at < opts.since:
                    continue
                for end in (e.src, e.dst):
                    if end not in seen:
                        seen[end] = _exportable(await self.items.get_system(org_id, end), opts)
                if seen[e.src] and seen[e.dst]:
                    keep.append(edge_row(e, alias))
            writer.write(keep)
            count += len(keep)
        return count


def _prepare(out_dir: Path) -> Path:
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ValidationError(f"{out_dir} exists and is not an empty directory")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.tmp-", dir=out_dir.parent))  # mode 0700


def _publish(staging: Path, out_dir: Path) -> None:
    if out_dir.exists():
        out_dir.rmdir()  # checked empty by _prepare
    staging.rename(out_dir)


async def run_export(  # noqa: PLR0913 - ports plus the request
    *,
    items: ItemRepository,
    maintenance: ItemMaintenance,
    edges: EdgeRepository,
    audit: AuditLog,
    clock: Clock,
    org_id: str,
    out_dir: Path,
    opts: ExportOptions,
    actor: str,
) -> ExportResult:
    check_format(opts.format)
    staging = await asyncio.to_thread(_prepare, out_dir)
    alias = Pseudonymiser(opts.pseudonymise)
    ext = "parquet" if opts.format == "parquet" else "jsonl"
    try:
        run = _Pass(items, maintenance, edges)
        counts: dict[str, int] = {}
        for name, columns, step in (
            ("items", ITEM_COLUMNS, run.write_items),
            ("edges", EDGE_COLUMNS, run.write_edges),
        ):
            writer = make_writer(opts.format, staging / f"{name}.{ext}", columns)
            try:
                counts[name] = await step(org_id, opts, alias, writer)
            finally:
                writer.close()
        manifest = _manifest(staging, org_id, opts, counts, clock.now())
        _write_manifest(staging / "manifest.json", manifest)
        _publish(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    await audit.append(
        AuditEvent(
            org_id,
            actor,
            ACTION,
            clock.now(),
            None,
            {
                "format": opts.format,
                "include_private": str(opts.include_private).lower(),
                "include_archived": str(opts.include_archived).lower(),
                "since": opts.since.isoformat() if opts.since else "",
                "pseudonymise": str(opts.pseudonymise).lower(),
                "items": str(counts["items"]),
                "edges": str(counts["edges"]),
            },
        )
    )
    return ExportResult(out_dir, counts["items"], counts["edges"], manifest)


def _manifest(
    staging: Path, org_id: str, opts: ExportOptions, counts: dict[str, int], now: datetime
) -> dict[str, Any]:
    files = {}
    for name in ("items", "edges"):
        path = staging / f"{name}.{'parquet' if opts.format == 'parquet' else 'jsonl'}"
        files[path.name] = {
            "rows": counts[name],
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "org": org_id,
        "created_at": now.isoformat(),
        "format": opts.format,
        "flags": {
            "include_private": opts.include_private,
            "include_archived": opts.include_archived,
            "since": opts.since.isoformat() if opts.since else None,
            "pseudonymise": opts.pseudonymise,
        },
        "counts": counts,
        "files": files,
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
