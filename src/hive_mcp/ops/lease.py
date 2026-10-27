"""One writer at a time: a lease file in the backup directory.

With the data on each container's own disk, two containers alive at once (a platform starting the
new revision before stopping the old one) would write diverging databases, and the older one's last
changes would be lost. The running instance therefore holds a lease (a small file it touches every
few seconds); a second instance that finds a live lease waits for its release (a clean shutdown
deletes it, after the final backup) and gives up with a clear message if it does not come. A lease
nobody has touched for a while belongs to a dead instance and is taken over.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING

from hive_mcp.domain.errors import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("hive.lease")
FILE = ".hive-lease"
HEARTBEAT_SECONDS = 10.0
STALE_SECONDS = 45.0
POLL_SECONDS = 2.0


class Lease:
    def __init__(self, directory: Path, owner: str | None = None) -> None:
        self._path = directory / FILE
        self.owner = owner or uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read(self) -> tuple[str, float] | None:
        try:
            data = json.loads(self._path.read_text())
            return str(data["owner"]), self._path.stat().st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            return None  # absent or unreadable: nobody holds it

    def _write(self) -> None:
        partial = self._path.with_name(f"{FILE}.{self.owner[:8]}")
        partial.write_text(json.dumps({"owner": self.owner, "pid": os.getpid()}))
        partial.replace(self._path)

    def acquire(self, wait_seconds: float) -> None:
        """Take the lease, waiting up to `wait_seconds` for a live holder to let go."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait_seconds
        announced = False
        while True:
            held = self._read()
            if held is None or held[0] == self.owner or time.time() - held[1] > STALE_SECONDS:
                self._write()
                time.sleep(0.2)  # two starters at once: the last writer wins, and the loser sees it
                current = self._read()
                if current and current[0] == self.owner:
                    self._start_heartbeat()
                    return
            if time.monotonic() >= deadline:
                raise ValidationError(
                    "another instance is using this backup directory (lease held); two instances "
                    "must not run at once. Stop it first, or wait for it to finish shutting down"
                )
            if not announced:
                log.warning("waiting for the running instance to release the lease")
                announced = True
            time.sleep(POLL_SECONDS)

    def _start_heartbeat(self) -> None:
        def beat() -> None:
            while not self._stop.wait(HEARTBEAT_SECONDS):
                held = self._read()
                if held is not None and held[0] != self.owner:
                    log.error("the lease was taken over by another instance")
                    return
                with contextlib.suppress(OSError):
                    self._write()

        self._thread = threading.Thread(target=beat, name="hive-lease", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        held = self._read()
        if held is not None and held[0] == self.owner:
            self._path.unlink(missing_ok=True)
