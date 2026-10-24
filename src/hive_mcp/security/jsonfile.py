"""A JSON document in one file: atomic owner-only writes; reads cached until it is replaced."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class AtomicJsonFile:
    def __init__(self, path: Path, default: Any) -> None:  # noqa: ANN401 - any JSON document
        self.path, self._default = path, default
        self._cache: tuple[tuple[int, int, int], Any] | None = None

    def read(self) -> Any:  # noqa: ANN401 - JSON document
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return json.loads(json.dumps(self._default))
        # Writes replace the file atomically (new inode), so this key changes even when two writes
        # share a coarse timestamp tick; a stale cache would keep a revoked token or user alive.
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if self._cache and self._cache[0] == key:
            return json.loads(json.dumps(self._cache[1]))  # callers get a private copy
        data = json.loads(self.path.read_text())
        self._cache = (key, data)
        return json.loads(json.dumps(data))

    def write(self, data: Any) -> None:  # noqa: ANN401 - JSON document
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._cache = None
