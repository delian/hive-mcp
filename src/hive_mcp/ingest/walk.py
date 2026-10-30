"""Deterministic, resumable folder walk for bulk ingestion (never leaves the root).

Order: depth first, the entries of each directory sorted by name. A cursor is the relative POSIX
path of the last file handled; resuming skips everything up to it without listing finished
directories again. Symlinks (to files or directories) and hidden entries are not followed.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def walk_after(root: Path, cursor: str | None) -> Iterator[tuple[str, int]]:
    """Yield `(relative path, size)` of regular files after `cursor`, in a stable order."""
    after = tuple(cursor.split("/")) if cursor else None
    yield from _walk(str(root), "", after)


def _entries(directory: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as it:
            return sorted((e for e in it if not e.name.startswith(".")), key=lambda e: e.name)
    except OSError:
        return []  # unreadable directory: skipped, the walk goes on


def _walk(directory: str, prefix: str, after: tuple[str, ...] | None) -> Iterator[tuple[str, int]]:
    depth = prefix.count("/")
    if after is not None and len(after) <= depth:
        after = None  # the cursor was a file where a directory is now: everything inside is new
    for entry in _entries(directory):
        if after is not None:
            if entry.name < after[depth]:
                continue
            if entry.name > after[depth]:
                after = None
        rel = f"{prefix}{entry.name}"
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(entry.path, rel + "/", after)
            elif entry.is_file(follow_symlinks=False):
                if after is not None and len(after) == depth + 1:
                    after = None  # this is the cursor file itself: already handled
                    continue
                yield rel, entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        if after is not None and rel == "/".join(after[: depth + 1]):
            after = None  # the cursor's directory is finished: everything later is new


def open_regular(root: Path, rel: str) -> BinaryIO:
    """Open a file below `root` without following a symlink at any step (race-safe)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        *parents, name = rel.split("/")
        for part in parents:
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        fd = os.open(name, flags, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"not a regular file: {rel}")
    return os.fdopen(fd, "rb")
