"""One file of a bulk run: read it without following links, ingest it, update the counters."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from hive_mcp.domain.errors import StorageBusy, ValidationError
from hive_mcp.ingest.extract import TEXT_SUFFIXES
from hive_mcp.ingest.walk import open_regular
from hive_mcp.services.bulk_state import BULK_TEXT_MAX_BYTES, MAX_ERRORS, PIECE_BYTES, Progress
from hive_mcp.services.ingest import MAX_FILE_BYTES

if TYPE_CHECKING:
    from hive_mcp.domain.principal import Principal
    from hive_mcp.domain.scope import Scope
    from hive_mcp.services.ingest import IngestService


def note(progress: Progress, text: str) -> None:
    if len(progress.errors) < MAX_ERRORS:
        progress.errors.append(text)


async def ingest_file(  # noqa: PLR0913, PLR0917 - one file of one run
    ingest: IngestService,
    who: Principal,
    scope: Scope | None,
    tags: tuple[str, ...],
    file: tuple[Path, str, int],  # folder, path below it, size
    progress: Progress,
) -> None:
    """Failures are counted and noted, never raised (except a busy database: retry the step).
    Over 50 MiB only plain text is accepted, stored in pieces; anything else is skipped."""
    root, rel, size = file
    suffix = PurePosixPath(rel.lower()).suffix
    text = suffix in TEXT_SUFFIXES
    if size > MAX_FILE_BYTES and (not text or size > BULK_TEXT_MAX_BYTES):
        limit = (BULK_TEXT_MAX_BYTES if text else MAX_FILE_BYTES) >> 20
        note(progress, f"skipped {rel}: {size >> 20} MiB is over the {limit} MiB limit")
        progress.skipped += 1
        return
    try:
        if size > MAX_FILE_BYTES:
            created = await _pieces(ingest, who, scope, tags, root, rel)
        else:
            data = await asyncio.to_thread(_read, root, rel)
            name = PurePosixPath(rel).name
            created = (await ingest.ingest(who, name, data, tags=tags, scope=scope)).created
    except StorageBusy:
        raise
    except Exception as exc:
        progress.failed += 1
        note(progress, f"{rel}: {str(exc)[:200] or exc.__class__.__name__}")
        return
    progress.done += 1
    progress.bytes += size
    progress.created += created


def _read(root: Path, rel: str) -> bytes:
    with open_regular(root, rel) as handle:
        return handle.read(MAX_FILE_BYTES + 1)


def _piece(handle: BinaryIO) -> bytes:
    data = handle.read(PIECE_BYTES)
    return data + handle.readline() if data and not data.endswith(b"\n") else data


async def _pieces(  # noqa: PLR0913, PLR0917 - same arguments as ingest_file
    ingest: IngestService,
    who: Principal,
    scope: Scope | None,
    tags: tuple[str, ...],
    root: Path,
    rel: str,
) -> int:
    """Documents `name.part0001.ext`, ... each cut at a line end: one piece in memory at a time."""
    path = PurePosixPath(rel)
    created = 0
    with await asyncio.to_thread(open_regular, root, rel) as handle:
        head = await asyncio.to_thread(handle.read, 8192)
        if head.startswith(b"%PDF-") or b"\x00" in head:
            raise ValidationError("binary file: only plain text may be this large")
        handle.seek(0)
        for number in itertools.count(1):
            piece = await asyncio.to_thread(_piece, handle)
            if not piece:
                break
            name = f"{path.stem}.part{number:04d}{path.suffix}"
            created += (await ingest.ingest(who, name, piece, tags=tags, scope=scope)).created
    return created
