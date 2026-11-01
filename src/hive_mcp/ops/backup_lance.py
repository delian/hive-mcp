"""The LanceDB vector store (`<data>/lance`) in a backup.

Lance files are immutable and a table version is published by writing a small manifest last, so
a copy that takes the data files first and the manifests last refers only to files it holds. The
copy is opened (rows counted) before it is trusted; a compaction that deletes files under the
copy makes that fail, and the copy is repeated. The index is derived data: when no consistent
copy can be made the backup goes ahead without it, says so, and `hive reindex` rebuilds it.
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("hive.backup")

DIRECTORY = "lance"
ATTEMPTS = 3
LAST = "_versions"  # table manifests: copied after everything they point to


def _copy_once(source: Path, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    files = sorted(p for p in source.rglob("*") if p.is_file())
    files.sort(key=lambda p: LAST in p.relative_to(source).parts)  # stable: manifests go last
    for path in files:
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)  # FileNotFoundError: a compaction removed it, try again


def _opens(directory: Path) -> bool:
    """True when every table of the copy can be read (or when lancedb is not installed to ask)."""
    try:
        import lancedb  # noqa: PLC0415 - optional extra
    except ImportError:
        return True
    try:
        db = lancedb.connect(str(directory))
        for name in db.table_names():
            db.open_table(name).count_rows()
    except Exception:  # any failure to read the copy means it is not usable
        return False
    return True


def snapshot(source: Path, dest: Path) -> bool:
    """Copy `source` to `dest`; False if no consistent copy could be made."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            _copy_once(source, dest)
        except OSError as exc:
            log.warning("lance copy attempt %d failed: %s", attempt, exc)
            continue
        if _opens(dest):
            return True
        log.warning("lance copy attempt %d could not be opened", attempt)
    shutil.rmtree(dest, ignore_errors=True)
    return False


def restore(stage: Path, data: Path) -> None:
    """Replace `<data>/lance` with the archived copy (copy first, swap after)."""
    partial, final = data / f"{DIRECTORY}.restoring", data / DIRECTORY
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(stage / DIRECTORY, partial)
    shutil.rmtree(final, ignore_errors=True)
    partial.replace(final)


def archive_members(data: Path, stage: Path) -> tuple[dict[str, Path], list[str]]:
    """The archive entries for `<data>/lance` (none if absent) and notes for the operator."""
    source = data / DIRECTORY
    if not source.is_dir():
        return {}, []
    staged = stage / DIRECTORY
    if not snapshot(source, staged):
        return {}, [
            "the LanceDB index could not be copied consistently and is not in the archive; "
            "run `hive reindex` after a restore"
        ]
    files = sorted(p for p in staged.rglob("*") if p.is_file())
    return {f"{DIRECTORY}/{p.relative_to(staged).as_posix()}": p for p in files}, []


def restore_into(stage: Path, data: Path) -> list[str]:
    """Bring the archived index back; tell the operator when the index here cannot be trusted."""
    if (stage / DIRECTORY).is_dir():
        restore(stage, data)
        return []
    if (data / DIRECTORY).is_dir():
        return [
            "the archive holds no LanceDB index but one exists here; it may not match the "
            "restored database: run `hive reindex`"
        ]
    return []
