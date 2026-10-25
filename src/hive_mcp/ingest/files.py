"""Finding files to ingest on disk."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def files_under(paths: list[Path]) -> Iterator[Path]:
    """Files given directly, plus regular files below directories (sorted; hidden entries and
    symlinks are skipped so a stray link cannot pull in files from elsewhere)."""
    for path in paths:
        if path.is_dir():
            for found in sorted(path.rglob("*")):
                parts = found.relative_to(path).parts
                if (
                    found.is_file()
                    and not found.is_symlink()
                    and not any(p.startswith(".") for p in parts)
                ):
                    yield found
        elif path.is_file():
            yield path
